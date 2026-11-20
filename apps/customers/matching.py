"""Dopasowanie nazwy klienta z luźnego tekstu (np. transkrypcji) do bazy klientów organizacji."""
import difflib
import re
import unicodedata

from .models import Customer

_STOP = {"szpital", "szpitala", "szpitalu", "spzoz", "sp", "zoz", "z", "o", "oo", "w", "we", "im", "imienia",
         "wojewodzki", "wojewodzkiego", "powiatowy", "powiatowego", "klinika", "kliniki", "centrum", "medyczne",
         "medycznego", "u", "na", "do", "dla", "i"}


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _tokens(text):
    return {t for t in normalize(text).split() if len(t) > 2 and t not in _STOP}


def _stem(token):
    return token[:5]  # prymitywny „stemming” na odmianę: radom/radomiu, lublin/lublinie


def match_customer(organization, text, min_score=0.5):
    """Zwraca (customer, score) najlepszego dopasowania albo (None, 0.0). Score 0..1."""
    words = _tokens(text)
    if not words:
        return None, 0.0
    stems = {_stem(w) for w in words}
    best, best_score = None, 0.0
    for customer in Customer.objects.for_org(organization).filter(is_active=True):
        name_tokens = _tokens(f"{customer.name} {customer.short_name} {customer.city}")
        if not name_tokens:
            continue
        hits = sum(1 for t in name_tokens if _stem(t) in stems)
        score = hits / len(name_tokens)
        short = normalize(customer.short_name).strip()
        if short and short in normalize(text):
            score = max(score, 0.95)
        if score < 1:
            ratio = difflib.SequenceMatcher(None, normalize(customer.name), normalize(text)).ratio()
            score = max(score, ratio * 0.8)
        if score > best_score:
            best, best_score = customer, score
    if best_score < min_score:
        return None, best_score
    return best, round(best_score, 2)
