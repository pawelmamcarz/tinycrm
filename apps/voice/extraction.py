"""Ekstrakcja z transkrypcji: klient, osoba kontaktowa, zadania, sprawy serwisowe, termin kontaktu.

Ścieżka główna: Claude przez `apps.core.ai.ask_json` (strict JSON). Wynik AI zawsze walidujemy
(nieznany typ → INNE, zła data → null, klient spoza organizacji → dopasowanie po nazwie).
Fallback (brak klucza / `_error`): heurystyka regułowa dla polskiej mowy potocznej handlowca.

Wynik `extract()` — znormalizowany dict:
    {customer_id|None, customer_guess, contact_person, summary,
     tasks: [{title, kind, due_date(ISO)|None, assignee_role, priority}],
     service: [{title, description, device_hint, parts: [{name, quantity}]}],
     follow_up_date(ISO)|None, engine: "ai"|"heurystyka"}
"""
import calendar
import logging
import re
from datetime import date, timedelta

from django.utils import timezone

from apps.core.ai import ask_json
from apps.customers.matching import match_customer, normalize
from apps.customers.models import Customer
from apps.tasks.models import TaskKind

logger = logging.getLogger(__name__)

ASSIGNEE_ROLES = ("BIURO", "HANDLOWIEC", "SERWIS")
KIND_VALUES = tuple(TaskKind.values)
MAX_CUSTOMERS_IN_PROMPT = 200
MAX_TASKS = 20
MAX_SERVICE = 10
MAX_PARTS = 10

WEEKDAYS_PL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]

# Domyślna rola wykonawcy dla typu zadania („handlowiec mówi, biuro odhacza”).
DEFAULT_ROLE_FOR_KIND = {
    "OFERTA": "BIURO",
    "ZAMOWIENIE": "BIURO",
    "PRZETARG": "BIURO",
    "KONTAKT": "HANDLOWIEC",
    "SERWIS": "SERWIS",
    "PRZEGLAD": "SERWIS",
    "INNE": "BIURO",
}


# ---------------------------------------------------------------------------------------------
# Daty względne
# ---------------------------------------------------------------------------------------------

_NUM_WORDS = {
    "jeden": 1, "jedna": 1, "jedną": 1, "jedno": 1, "dwa": 2, "dwie": 2, "dwóch": 2, "trzy": 3, "cztery": 4,
    "pięć": 5, "piec": 5, "sześć": 6, "szesc": 6, "siedem": 7, "osiem": 8, "dziewięć": 9, "dziewiec": 9,
    "dziesięć": 10, "dziesiec": 10, "kilka": 3, "parę": 2, "pare": 2,
}
_NUM = r"(\d{1,2}|" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")"

_WEEKDAY_PATTERNS = [
    (0, r"poniedzia[łl]\w*"),
    (1, r"wtor(?:ek|ku|kiem)"),
    (2, r"[śs]rod(?:a|ę|e|y|zie|ą)"),
    (3, r"czwart(?:ek|ku|kiem)"),
    (4, r"pi[ąa]t(?:ek|ku|kiem)"),
    (5, r"sobot(?:a|ę|e|y|ie|ą)"),
    (6, r"niedziel(?:a|ę|e|i|ą)"),
]
_PREP = r"(?:(?:do|w|we|na|od|po|przed|najpóźniej\s+do|najpozniej\s+do)\s+)?"


def _num_value(token):
    token = token.lower()
    if token.isascii() and token.isdigit():
        return int(token)
    return _NUM_WORDS.get(token)


def _end_of_month(today):
    return today.replace(day=calendar.monthrange(today.year, today.month)[1])


def _next_weekday(today, weekday):
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _explicit_date(day, month, year, today):
    try:
        if year is None:
            candidate = date(today.year, month, day)
            if candidate < today:
                candidate = date(today.year + 1, month, day)
            return candidate
        year = int(year)
        if year < 100:
            year += 2000
        return date(year, month, day)
    except ValueError:
        return None


# Kontekst przed „14.11” oznaczający godzinę albo wersję, a nie datę („o 14.11”, „godz. 10.05”, „wersja 2.1”).
_NOT_DATE_BEFORE = re.compile(r"(?:\bo|\bgodz\.?|\bgodzin\w*|\bokoło|\bokolo|\bwersj\w*|\bv)\s*$", re.IGNORECASE)


def _dotted_date(match, today):
    if _NOT_DATE_BEFORE.search(match.string[:match.start(1)]):
        return None
    return _explicit_date(int(match.group(1)), int(match.group(2)), match.group(3), today)


def _next_week_monday(today):
    return _next_weekday(today, 0)


def _date_rules(today):
    """Lista (regex, funkcja(match) -> date|None). Regexy z opcjonalnym przyimkiem (do/w/na...).

    Kolejność ma znaczenie: przy nakładaniu się dopasowań wygrywa wcześniejsza reguła.
    """
    rules = []
    # „w przyszłym tygodniu w środę” / „w środę w przyszłym tygodniu” → środa przyszłego tygodnia.
    for weekday, pattern in _WEEKDAY_PATTERNS:
        rules.append((rf"(?:w\s+)?przysz[łl]ym\s+tygodniu\s*,?\s+{_PREP}\b{pattern}\b",
                      lambda m, wd=weekday: _next_week_monday(today) + timedelta(days=wd)))
        rules.append((rf"{_PREP}\b{pattern}\s+(?:w\s+)?przysz[łl]ym\s+tygodniu\b",
                      lambda m, wd=weekday: _next_week_monday(today) + timedelta(days=wd)))
    rules += [
        (rf"{_PREP}\b(\d{{1,2}})\.(\d{{1,2}})(?:\.(\d{{2,4}}))?\b", lambda m: _dotted_date(m, today)),
        (rf"{_PREP}\bpojutrze\b", lambda m: today + timedelta(days=2)),
        (rf"{_PREP}\bjutr(?:o|a)\b", lambda m: today + timedelta(days=1)),
        (rf"{_PREP}\b(?:dzi[śs]|dzisiaj)\b", lambda m: today),
        (rf"\bza\s+tydzie[ńn]\b", lambda m: today + timedelta(days=7)),
        (rf"\bza\s+{_NUM}\s+dni\b", lambda m: _plus_days(today, m.group(1), 1)),
        (rf"\bza\s+{_NUM}\s+tygodni(?:e)?\b", lambda m: _plus_days(today, m.group(1), 7)),
        (rf"\bza\s+dwa\s+tygodnie\b", lambda m: today + timedelta(days=14)),
        (r"\b(?:na|do|pod)\s+koni?ec\s+miesi[ąa]ca\b|\bdo\s+ko[ńn]ca\s+miesi[ąa]ca\b",
         lambda m: _end_of_month(today)),
        (r"\b(?:w\s+)?przysz[łl]ym\s+tygodniu\b", lambda m: _next_weekday(today, 0)),
    ]
    for weekday, pattern in _WEEKDAY_PATTERNS:
        rules.append((rf"{_PREP}\b{pattern}\b", lambda m, wd=weekday: _next_weekday(today, wd)))
    return rules


def _plus_days(today, token, multiplier):
    value = _num_value(token)
    if value is None:
        return None
    return today + timedelta(days=value * multiplier)


def find_dates(text, today):
    """Wszystkie daty w tekście: lista (start, end, date) posortowana po pozycji, bez nakładania."""
    found = []
    for pattern, resolver in _date_rules(today):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = resolver(match)
            if value is None:
                continue
            if any(not (match.end() <= s or match.start() >= e) for s, e, _d in found):
                continue
            found.append((match.start(), match.end(), value))
    return sorted(found)


def parse_relative_date(text, today):
    """Pierwsza data wymieniona w tekście (albo None)."""
    dates = find_dates(text or "", today)
    return dates[0][2] if dates else None


def _strip_spans(text, spans):
    out, last = [], 0
    for start, end, _value in spans:
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    return re.sub(r"\s{2,}", " ", "".join(out)).strip(" ,;:-")


# ---------------------------------------------------------------------------------------------
# Heurystyka
# ---------------------------------------------------------------------------------------------

# Kolejność ma znaczenie: pierwsze trafienie wygrywa (serwis przed zamówieniem — „zamówić część”).
_KIND_KEYWORDS = [
    ("PRZETARG", ("przetarg", "siwz", " swz")),
    ("PRZEGLAD", ("przeglad",)),
    ("SERWIS", ("awari", "napraw", "serwis", "czesc", "czesci", "uszkodz", "padl", "zepsu", "nie dziala",
                "usterk", "wymien", "wymian", "reklamac")),
    ("OFERTA", ("ofert", "wycen")),
    ("ZAMOWIENIE", ("zamow",)),
    ("KONTAKT", ("zadzwo", "kontakt", "umow", "spotkan", "odezwa", "przypomni")),
]
_ACTION_HINTS = ("trzeba", "nalezy", "musz", "musimy", "prosz", "przygotow", "wysla", "sprawdz", "dostarcz",
                 "potrzeb", "przeslac", "zalatw")
_FAILURE_HINTS = ("awari", "padl", "zepsu", "nie dziala", "uszkodz", "usterk")
_URGENT_HINTS = ("pilne", "pilnie", "natychmiast", "asap", "jak najszybciej", "na cito", "priorytet")

_DEVICES = [
    (r"\busg\b", "USG"), (r"\bekg\b", "EKG"), (r"\bkardiomonitor\w*", "kardiomonitor"),
    (r"\bdefibrylator\w*", "defibrylator"), (r"\brespirator\w*", "respirator"),
    (r"\bpomp\w*\s+infuzyjn\w*", "pompa infuzyjna"), (r"\b(?:aparat\w*\s+)?rtg\b", "RTG"),
    (r"\btomograf\w*", "tomograf"), (r"\brezonans\w*", "rezonans"), (r"\bkolposkop\w*", "kolposkop"),
    (r"\bendoskop\w*", "endoskop"), (r"\bgastroskop\w*", "gastroskop"), (r"\bkolonoskop\w*", "kolonoskop"),
    (r"\binkubator\w*", "inkubator"), (r"\bssak\w*", "ssak"), (r"\bautoklaw\w*", "autoklaw"),
    (r"\bsterylizator\w*", "sterylizator"), (r"\blamp\w*\s+operacyjn\w*", "lampa operacyjna"),
    (r"\bst[óo]\w*\s+operacyjn\w*", "stół operacyjny"), (r"\bholter\w*", "holter"),
    (r"\bspirometr\w*", "spirometr"), (r"\bpulsoksymetr\w*", "pulsoksymetr"), (r"\bdiatermi\w*", "diatermia"),
    (r"\bkardiotokograf\w*|\bktg\b", "KTG"), (r"\bmonitor\w*", "monitor"),
]
_LOCATION_RE = re.compile(
    r"\bna\s+(oddziale\s+\w+|bloku(?:\s+operacyjnym)?|sorze|izbie\s+przyj\w+|"
    r"intensywnej\s+terapii|\w*(?:logii|chirurgii|pediatrii|ortopedii|okulistyce|internie|neonatologii))\b",
    re.IGNORECASE,
)
_PART_TRIGGER_RE = re.compile(
    r"(?:zam[óo]wi[ćc]\w*|zam[óo]wienie|cz[ęe][śs][ćc]i?|potrzebn\w+\s+(?:jest|s[ąa])|wymieni[ćc]|wymian[aęy])"
    r"\s*:?\s+(.+?)(?=$|[,.;!?]|\s+(?:do|dla|bo|ale|i|oraz|na|żeby|zeby|aby|który|która|które|bo|z\s+uwagi)\b)",
    re.IGNORECASE,
)
_GENERIC_PART_WORDS = {"część", "części", "czesc", "czesci", "nową", "nowa", "nowe", "nowy", "nowej", "nowych",
                       "jakąś", "jakas", "zamienną", "zamienne", "zamiennej", "zamienna", "sztuk", "szt", "szt.",
                       "x"}
_CONTACT_RE = re.compile(
    r"\b(?i:u|z|ze|do|od|dla)\s+"
    r"(?i:(doktor(?:a|em|em)?|dr\.?|pan(?:a|em)?|pani|panią|ordynator(?:a|em)?|kierownik(?:a|iem)?|"
    r"kierowniczk\w+|profesor(?:a|em)?|prof\.?|dyrektor(?:a|em)?|oddziałow\w+|pielęgniark\w+))\s+"
    r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?)"
)
_FACILITY_RE = re.compile(
    r"\b(?:w|we|u|z)\s+((?:szpital|klinic|klinik|przychodn|gabinec|gabinet|centrum|poradn|ośrod|osrod|zakład|"
    r"zaklad|hospic|laborator|instytu|sanatori)\w*(?:\s+(?!(?:u|do|i|na|bo|ale|oraz)\b)[\wąćęłńóśźż-]+){0,4})",
    re.IGNORECASE,
)


def _contains(norm_text, keywords):
    return any(k in norm_text for k in keywords)


def classify(clause):
    norm = " " + " ".join(normalize(clause).split()) + " "
    for kind, keywords in _KIND_KEYWORDS:
        if _contains(norm, keywords):
            return kind
    if _contains(norm, _ACTION_HINTS):
        return "INNE"
    return None


def _split_clauses(text):
    sentences = re.split(r"(?<=[.!?;])\s+|\n+", text or "")
    clauses = []
    for sentence in sentences:
        parts = re.split(r",?\s+(?:a\s+(?:potem|poza\s+tym|także|takze|też|tez)|ponadto|poza\s+tym|oprócz\s+tego|"
                         r"oprocz\s+tego)\s+", sentence, flags=re.IGNORECASE)
        clauses.extend(p.strip(" .!?;,") for p in parts if p and p.strip(" .!?;,"))
    return clauses


def _to_nominative(word):
    """Prosta heurystyka biernik → mianownik dla nazw części: głowicę konweksową → głowica konweksowa."""
    if not word.isalpha() or word != word.lower():
        return word
    if word.endswith("ę") or word.endswith("ą"):
        return word[:-1] + "a"
    return word


def _clean_part(raw):
    tokens = raw.strip(" :,-").split()
    quantity = 1
    cleaned = []
    for i, token in enumerate(tokens):
        low = token.lower().rstrip(",.")
        if not cleaned:
            match = re.fullmatch(r"(\d{1,3})\s*(?:x|szt\.?)?", low)
            if match:
                quantity = int(match.group(1))
                continue
            if low in _NUM_WORDS and i + 1 < len(tokens):
                quantity = _NUM_WORDS[low]
                continue
            if low in _GENERIC_PART_WORDS:
                continue
        if low in ("szt", "szt.", "sztuki", "sztuk"):
            continue
        cleaned.append(_to_nominative(token.rstrip(",.")))
    name = " ".join(cleaned[:6]).strip()
    if not name or name.lower() in _GENERIC_PART_WORDS:
        return None
    return {"name": name[:200], "quantity": max(1, min(quantity, 999))}


def parse_parts(clause):
    parts, seen = [], set()
    for match in _PART_TRIGGER_RE.finditer(clause):
        part = _clean_part(match.group(1))
        if part and part["name"].lower() not in seen:
            seen.add(part["name"].lower())
            parts.append(part)
    return parts[:MAX_PARTS]


def _device_hint(text):
    device = None
    for pattern, label in _DEVICES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            device = label
            break
    location = _LOCATION_RE.search(text)
    if device and location:
        return f"{device} — na {location.group(1).lower()}"
    if device:
        return device
    return f"na {location.group(1).lower()}" if location else ""


_MALE_TITLES = {"doktora": "dr", "doktorem": "dr", "pana": "pan", "panem": "pan", "ordynatora": "ordynator",
                "ordynatorem": "ordynator", "kierownika": "kierownik", "kierownikiem": "kierownik",
                "profesora": "prof.", "profesorem": "prof.", "dyrektora": "dyrektor", "dyrektorem": "dyrektor"}


def _title_label(title):
    low = title.lower().rstrip(".")
    if low in _MALE_TITLES:
        return _MALE_TITLES[low], "m"
    if low in ("pani", "panią"):
        return "pani", "f"
    if low.startswith("kierowniczk"):
        return "kierowniczka", "f"
    if low.startswith("oddziałow"):
        return "oddziałowa", "f"
    if low.startswith("pielęgniark"):
        return "pielęgniarka", "f"
    mapping = {"doktor": "dr", "dr": "dr", "pan": "pan", "ordynator": "ordynator", "kierownik": "kierownik",
               "profesor": "prof.", "prof": "prof.", "dyrektor": "dyrektor"}
    return mapping.get(low, low), "?"


def _surname_nominative(surname, gender):
    for suffix, repl in (("skiego", "ski"), ("ckiego", "cki"), ("dzkiego", "dzki"), ("skiej", "ska"),
                         ("ckiej", "cka"), ("dzkiej", "dzka")):
        if surname.endswith(suffix):
            return surname[: -len(suffix)] + repl
    if gender == "m":
        for suffix in ("iem", "em"):
            if surname.endswith(suffix) and len(surname) > len(suffix) + 2:
                return surname[: -len(suffix)]
        if surname.endswith("a") and len(surname) > 3:
            return surname[:-1]
    if gender == "f" and surname.endswith("ą"):
        return surname[:-1] + "a"
    return surname


def find_contact_person(text):
    match = _CONTACT_RE.search(text or "")
    if not match:
        return ""
    label, gender = _title_label(match.group(1))
    return f"{label} {_surname_nominative(match.group(2), gender)}"


def find_customer_guess(text):
    match = _FACILITY_RE.search(text or "")
    if match:
        return match.group(1).strip(" ,.")[:120]
    return ""


def _priority(clause):
    return 3 if _contains(" ".join(normalize(clause).split()), _URGENT_HINTS) else 2


def _role_for(kind, clause):
    norm = normalize(clause)
    if re.search(r"\bbiur\w*", norm):
        return "BIURO"
    if kind not in ("SERWIS", "PRZEGLAD") and re.search(r"\bserwis\w*\s+(?:niech|ma|musi)\b", norm):
        return "SERWIS"
    return DEFAULT_ROLE_FOR_KIND.get(kind, "BIURO")


def _capitalize(text):
    return text[:1].upper() + text[1:] if text else text


def _task_title(kind, clause_wo_dates):
    text = clause_wo_dates.strip(" ,.;")
    if kind == "OFERTA":
        match = re.search(r"\bofert\w*\s+((?:na|dla)\s+.+)", text, flags=re.IGNORECASE)
        if match:
            return _capitalize(f"oferta {match.group(1)}")[:255]
    if kind == "PRZETARG":
        match = re.search(r"\bprzetarg\w*\s+((?:na|dla)\s+.+)", text, flags=re.IGNORECASE)
        if match:
            return _capitalize(f"przetarg {match.group(1)}")[:255]
    return _capitalize(text)[:255]


def heuristic_extract(organization, transcript, today):
    transcript = transcript or ""
    tasks, service = [], []
    for index, clause in enumerate(_split_clauses(transcript)):
        kind = classify(clause)
        if kind is None:
            continue
        dates = find_dates(clause, today)
        due = dates[0][2] if dates else None
        stripped = _strip_spans(clause, dates)
        if kind == "SERWIS":
            device = _device_hint(clause)
            failure = _contains(" ".join(normalize(clause).split()), _FAILURE_HINTS)
            device_name = device.split(" — ")[0] if device else ""
            if device_name and not device_name.startswith("na "):
                title = f"{'Awaria' if failure else 'Serwis'}: {device_name}"
            else:
                title = _capitalize(stripped)[:120]
            parts = parse_parts(clause)
            service.append({"title": title, "description": clause.strip(), "device_hint": device, "parts": parts})
            if device_name and not device_name.startswith("na "):
                part_names = ", ".join(p["name"] for p in parts)
                stripped = f"{title} — {part_names}" if part_names else title
        if kind == "INNE" and index == 0 and not dates:
            # Pierwsze zdanie to zwykle kontekst wizyty („Byłem w…”) — nie zadanie.
            continue
        tasks.append({
            "title": _task_title(kind, stripped) or _capitalize(clause)[:255],
            "kind": kind,
            "due_date": due.isoformat() if due else None,
            "assignee_role": _role_for(kind, clause),
            "priority": _priority(clause),
        })
    customer, _score = match_customer(organization, transcript)
    follow_ups = sorted(t["due_date"] for t in tasks if t["kind"] == "KONTAKT" and t["due_date"])
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript.strip()) if s.strip()]
    summary = " ".join(sentences[:2])[:300]
    return {
        "customer_id": customer.pk if customer else None,
        "customer_guess": find_customer_guess(transcript) or (str(customer) if customer else ""),
        "contact_person": find_contact_person(transcript),
        "summary": summary,
        "tasks": tasks[:MAX_TASKS],
        "service": service[:MAX_SERVICE],
        "follow_up_date": follow_ups[0] if follow_ups else None,
        "engine": "heurystyka",
    }


# ---------------------------------------------------------------------------------------------
# Ścieżka AI
# ---------------------------------------------------------------------------------------------

SYSTEM_PROMPT = """Jesteś asystentem biura handlowego dystrybutora sprzętu medycznego w Polsce.
Dostajesz transkrypcję notatki głosowej handlowca nagranej po wizycie u klienta (szpital, klinika, gabinet).
Wyodrębnij z niej: klienta, osobę kontaktową, krótkie podsumowanie, zadania do wykonania i sprawy serwisowe.

Dzisiaj jest {today} ({weekday}). Daty względne („do piątku”, „w poniedziałek”, „jutro”, „za tydzień”,
„na koniec miesiąca”) zamień na konkretne daty ISO (RRRR-MM-DD) liczone od dzisiaj (najbliższy przyszły dzień).

Zwróć JSON o dokładnie takiej strukturze:
{{
  "customer_id": <id klienta z listy albo null, gdy nie ma pewności>,
  "customer_guess": "<nazwa klienta tak, jak padła w notatce>",
  "contact_person": "<osoba kontaktowa, np. dr Nowak, albo pusty tekst>",
  "summary": "<1-2 zdania podsumowania wizyty>",
  "tasks": [{{"title": "<krótko, w trybie polecenia>", "kind": "<jeden z: {kinds}>",
             "due_date": "<RRRR-MM-DD albo null>", "assignee_role": "<BIURO|HANDLOWIEC|SERWIS>",
             "priority": <1 niski, 2 normalny, 3 pilny>}}],
  "service": [{{"title": "<krótki tytuł zgłoszenia>", "description": "<opis usterki>",
               "device_hint": "<urządzenie i lokalizacja>", "parts": [{{"name": "<część w mianowniku>",
               "quantity": <liczba>}}]}}],
  "follow_up_date": "<RRRR-MM-DD następnego kontaktu z klientem albo null>"
}}
Zasady: oferty, zamówienia i przetargi przygotowuje BIURO; telefony i spotkania z klientem robi HANDLOWIEC;
awarie, naprawy i przeglądy — SERWIS. Każda awaria lub potrzebna część to osobna pozycja w "service".
Nie wymyślaj zadań, których nie ma w notatce.

Klienci organizacji (id: nazwa, miasto):
{customers}"""


def _customers_for_prompt(organization):
    rows = (Customer.objects.for_org(organization).filter(is_active=True)
            .order_by("name").values_list("pk", "name", "city")[:MAX_CUSTOMERS_IN_PROMPT])
    return "\n".join(f"{pk}: {name}" + (f", {city}" if city else "") for pk, name, city in rows) or "(brak)"


def _parse_iso_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _clean_str(value, limit):
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:limit]


def _clean_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _resolve_customer_id(organization, raw_id, guess, transcript):
    pk = None
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        pk = raw_id
    elif isinstance(raw_id, str) and raw_id.strip().isascii() and raw_id.strip().isdigit():
        pk = int(raw_id.strip())
    if pk is not None and Customer.objects.for_org(organization).filter(pk=pk, is_active=True).exists():
        return pk
    for text in (guess, transcript):
        if text:
            customer, _score = match_customer(organization, text)
            if customer:
                return customer.pk
    return None


def clean_tasks(raw_tasks):
    out = []
    if not isinstance(raw_tasks, list):
        return out
    for item in raw_tasks[:MAX_TASKS]:
        if not isinstance(item, dict):
            continue
        title = _clean_str(item.get("title"), 255)
        if not title:
            continue
        kind = str(item.get("kind") or "").upper()
        kind = kind if kind in KIND_VALUES else "INNE"
        role = str(item.get("assignee_role") or "").upper()
        role = role if role in ASSIGNEE_ROLES else DEFAULT_ROLE_FOR_KIND.get(kind, "BIURO")
        due = _parse_iso_date(item.get("due_date"))
        out.append({"title": title, "kind": kind, "due_date": due.isoformat() if due else None,
                    "assignee_role": role, "priority": _clean_int(item.get("priority"), 2, 1, 3)})
    return out


def clean_service(raw_service):
    out = []
    if not isinstance(raw_service, list):
        return out
    for item in raw_service[:MAX_SERVICE]:
        if not isinstance(item, dict):
            continue
        title = _clean_str(item.get("title"), 255)
        description = _clean_str(item.get("description"), 2000)
        if not title and not description:
            continue
        parts = []
        raw_parts = item.get("parts") if isinstance(item.get("parts"), list) else []
        for part in raw_parts[:MAX_PARTS]:
            if isinstance(part, str):
                part = {"name": part}
            if not isinstance(part, dict):
                continue
            name = _clean_str(part.get("name"), 200)
            if name:
                parts.append({"name": name, "quantity": _clean_int(part.get("quantity"), 1, 1, 999)})
        out.append({"title": title or description[:120], "description": description,
                    "device_hint": _clean_str(item.get("device_hint"), 200), "parts": parts})
    return out


def normalize_ai_result(organization, raw, transcript):
    guess = _clean_str(raw.get("customer_guess"), 200)
    follow_up = _parse_iso_date(raw.get("follow_up_date"))
    return {
        "customer_id": _resolve_customer_id(organization, raw.get("customer_id"), guess, transcript),
        "customer_guess": guess,
        "contact_person": _clean_str(raw.get("contact_person"), 200),
        "summary": _clean_str(raw.get("summary"), 1000),
        "tasks": clean_tasks(raw.get("tasks")),
        "service": clean_service(raw.get("service")),
        "follow_up_date": follow_up.isoformat() if follow_up else None,
        "engine": "ai",
    }


def ai_extract(organization, transcript, author, today):
    system = SYSTEM_PROMPT.format(today=today.isoformat(), weekday=WEEKDAYS_PL[today.weekday()],
                                  kinds=", ".join(KIND_VALUES), customers=_customers_for_prompt(organization))
    author_line = f"Autor notatki: {author} (handlowiec).\n" if author else ""
    raw = ask_json(system, f"{author_line}Transkrypcja notatki:\n\"\"\"\n{transcript}\n\"\"\"")
    if not isinstance(raw, dict) or raw.get("_error"):
        return None, (raw or {}).get("_error", "invalid_response") if isinstance(raw, dict) else "invalid_response"
    return normalize_ai_result(organization, raw, transcript), ""


def extract(organization, transcript, author=None, today=None):
    """Ekstrakcja z transkrypcji. AI, a przy `_error`/braku klucza — heurystyka. Nigdy nie rzuca."""
    today = today or timezone.localdate()
    transcript = (transcript or "").strip()
    if not transcript:
        return heuristic_extract(organization, "", today)
    try:
        result, error = ai_extract(organization, transcript, author, today)
    except Exception as exc:  # noqa: BLE001 — błąd walidacji wyniku AI nie może zatrzymać notatki
        logger.warning("Ekstrakcja AI nieudana: %s", exc.__class__.__name__)
        result, error = None, "exception"
    if result is not None:
        return result
    if error and error != "no_api_key":
        logger.info("Ekstrakcja AI niedostępna (%s) — heurystyka", error)
    return heuristic_extract(organization, transcript, today)
