"""Dopasowanie nazwy klienta z luźnego tekstu (np. transkrypcji) do bazy klientów organizacji.

Handlowiec mówi „byłem w szpitalu w Radomiu u ordynatora” — mamy znaleźć „Szpital Specjalistyczny
w Radomiu”. Polska odmiana (Radom/Radomiu, klinika/klinice, Zdrowie/Zdrowiu) jest obsługiwana przez
porównanie wspólnego prefiksu tokenów, a nie przez pełny stemmer.

Punktacja (0..1) dla klienta:
- tokeny wyróżniające (nazwa + nazwa skrócona bez słów ogólnych, typu placówki i miasta) — waga 0.7,
- miasto — 0.2, typ placówki (szpital/klinika/przychodnia…) — 0.1,
- dolna granica „lokalizacyjna”: miasto + typ = 0.55, samo miasto = 0.4
  (gdy klient nie ma tokenów wyróżniających: miasto + typ = 0.8, samo miasto = 0.6),
- nazwa skrócona wypowiedziana w całości = 0.95,
- remis kilku klientów bez dopasowania tokenów wyróżniających obniża wynik (niejednoznaczność).
"""
import difflib
import re
import unicodedata

from .models import Customer

# Słowa ogólne — nie odróżniają klientów. Lista jawnych form (bez dopasowania prefiksowego),
# żeby np. „zdrowie” nie zostało uznane za „zdrowotnej”.
_STOP = {
    "spzoz", "zoz", "nzoz", "sp", "z", "o", "oo", "w", "we", "im", "imienia", "u", "na", "do", "dla", "i", "oraz",
    "the", "ul", "al", "pod", "przy", "nad",
    "wojewodzki", "wojewodzkiego", "wojewodzkim", "wojewodzka", "wojewodzkiej",
    "powiatowy", "powiatowego", "powiatowym", "powiatowa", "powiatowej",
    "miejski", "miejskiego", "miejskim", "miejska", "miejskiej",
    "specjalistyczny", "specjalistycznego", "specjalistycznym", "specjalistyczna", "specjalistycznej",
    "kliniczny", "klinicznego", "klinicznym", "kliniczna", "klinicznej",
    "uniwersytecki", "uniwersyteckiego", "uniwersyteckim", "uniwersytecka", "uniwersyteckiej",
    "regionalny", "regionalnego", "regionalnym", "regionalna", "regionalnej",
    "samodzielny", "samodzielnego", "samodzielnym", "publiczny", "publicznego", "publicznym",
    "niepubliczny", "niepublicznego", "niepublicznym", "zespolony", "zespolonego", "zespolonym",
    "opieki", "zdrowotnej", "medyczne", "medycznego", "medycznym", "medyczny", "medyczna", "medycznej",
    "spolka", "spolki", "spolce", "akcyjna", "ograniczona", "odpowiedzialnoscia", "sa",
    "bylem", "bylam", "bylismy", "jestem", "bede", "dzisiaj", "dzis", "wczoraj", "jutro",
}

# Typy placówek: prefiks tokenu -> klucz typu (klinika/klinice/klinikach -> "klinika").
_TYPE_PREFIXES = (
    ("szpital", "szpital"), ("klinik", "klinika"), ("klinic", "klinika"), ("przychodn", "przychodnia"),
    ("gabinet", "gabinet"), ("centr", "centrum"), ("poradn", "poradnia"), ("osrod", "osrodek"),
    ("zaklad", "zaklad"), ("hospic", "hospicjum"), ("laborator", "laboratorium"), ("instytut", "instytut"),
    ("ambulatori", "ambulatorium"), ("sanatori", "sanatorium"), ("aptek", "apteka"), ("aptec", "apteka"),
)


def normalize(text):
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _words(text):
    return [t for t in normalize(text).split() if len(t) > 2 and t not in _STOP]


def _type_of(token):
    for prefix, key in _TYPE_PREFIXES:
        if token.startswith(prefix):
            return key
    return None


def _same_word(a, b):
    """Czy dwa tokeny to (prawdopodobnie) ten sam wyraz w innej formie: Radom/Radomiu, Łódź/Łodzi."""
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(4, min(len(a), len(b)) - 2)


def _hit(token, words):
    return any(_same_word(token, w) for w in words)


def _profile(customer):
    city_tokens = set(_words(customer.city))
    types, distinctive = set(), set()
    for token in _words(f"{customer.name} {customer.short_name}"):
        kind = _type_of(token)
        if kind:
            types.add(kind)
        elif not _hit(token, city_tokens):
            distinctive.add(token)
    return distinctive, city_tokens, types


def _short_name_in_text(short_name, text_norm):
    short = " ".join(normalize(short_name).split())
    if len(short) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", text_norm) is not None


def score_customer(customer, text):
    """Wynik 0..1 dopasowania tekstu do klienta oraz flaga, czy trafiono tokeny wyróżniające."""
    text_norm = " ".join(normalize(text).split())
    words = _words(text)
    text_types = {k for k in (_type_of(w) for w in words) if k}
    distinctive, city_tokens, types = _profile(customer)

    city_hit = 1.0 if city_tokens and all(_hit(t, words) for t in city_tokens) else 0.0
    type_hit = 1.0 if types & text_types else 0.0

    if distinctive:
        hits = sum(1 for t in distinctive if _hit(t, words))
        name_part = hits / len(distinctive)
        score = 0.7 * name_part + 0.2 * city_hit + 0.1 * type_hit if hits else 0.0
        location = 0.55 if (city_hit and type_hit) else 0.4 * city_hit
        score = max(score, location)
        strong = hits > 0
    else:
        score = 0.8 if (city_hit and type_hit) else 0.6 * city_hit
        strong = False

    if customer.short_name and _short_name_in_text(customer.short_name, text_norm):
        score, strong = max(score, 0.95), True

    # Tekst krótki (np. wpisany w wyszukiwarkę) — podobieństwo całości nazwy.
    name_norm = " ".join(normalize(customer.name).split())
    if score < 0.9 and name_norm and len(text_norm) <= 2 * len(name_norm):
        ratio = difflib.SequenceMatcher(None, name_norm, text_norm).ratio()
        if ratio * 0.8 > score:
            score, strong = ratio * 0.8, strong or ratio > 0.85
    return min(score, 1.0), strong


def match_customer(organization, text, min_score=0.5):
    """Zwraca (customer, score) najlepszego dopasowania albo (None, score). Score 0..1.

    Szuka wyłącznie wśród aktywnych klientów podanej organizacji (izolacja organizacji).
    """
    if not _words(text):
        return None, 0.0
    scored = []
    for customer in Customer.objects.for_org(organization).filter(is_active=True):
        score, strong = score_customer(customer, text)
        if score > 0:
            scored.append((score, strong, customer))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, best_strong, best = scored[0]
    # Niejednoznaczność: kilku klientów z identycznym wynikiem, bez trafienia w nazwę własną.
    if len(scored) > 1 and abs(scored[1][0] - best_score) < 1e-9 and not (best_strong and not scored[1][1]):
        best_score *= 0.8
    best_score = round(best_score, 2)
    if best_score < min_score:
        return None, best_score
    return best, best_score
