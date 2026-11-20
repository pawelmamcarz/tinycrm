from django import template

register = template.Library()

_STATUS_BADGES = {
    "NOWE": "badge-info",
    "PRZYPISANE": "badge-primary",
    "W_TOKU": "badge-warning",
    "CZEKA": "badge-secondary",
    "ODLOZONE": "badge-ghost",
    "ZROBIONE": "badge-success",
    "ODRZUCONE": "badge-error",
}

_STATUS_BUTTONS = {
    "ZROBIONE": "btn-success",
    "ODRZUCONE": "btn-error btn-outline",
    "W_TOKU": "btn-warning",
}


@register.filter
def status_badge(status):
    return _STATUS_BADGES.get(status, "badge-ghost")


@register.filter
def status_button(status):
    return _STATUS_BUTTONS.get(status, "btn-outline")
