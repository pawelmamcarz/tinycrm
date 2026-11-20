from django import template

register = template.Library()

_BADGES = {
    # zgłoszenia
    "NOWE": "badge-info",
    "W_TOKU": "badge-warning",
    "CZEKA_NA_CZESC": "badge-secondary",
    "ZAKONCZONE": "badge-success",
    "ANULOWANE": "badge-ghost",
    # przeglądy
    "ZAPLANOWANY": "badge-info",
    "UMOWIONY": "badge-primary",
    "WYKONANY": "badge-success",
    "ANULOWANY": "badge-ghost",
    # części
    "DO_ZAMOWIENIA": "badge-error",
    "ZAMOWIONE": "badge-warning",
    "DOSTARCZONE": "badge-info",
    "ZAMONTOWANE": "badge-success",
}

_BUTTONS = {
    "ZAKONCZONE": "btn-success",
    "WYKONANY": "btn-success",
    "ZAMONTOWANE": "btn-success",
    "DOSTARCZONE": "btn-info",
    "ZAMOWIONE": "btn-warning",
    "W_TOKU": "btn-warning",
    "UMOWIONY": "btn-primary",
    "ANULOWANE": "btn-error btn-outline",
    "ANULOWANY": "btn-error btn-outline",
}


@register.filter
def service_badge(status):
    return _BADGES.get(status, "badge-ghost")


@register.filter
def service_button(status):
    return _BUTTONS.get(status, "btn-outline")
