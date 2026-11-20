from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

ROLE_HOMES = {
    "HANDLOWIEC": "pwa:home",
    "BIURO": "tasks:office",
    "SERWIS": "service:dashboard",
    "SZEF": "tasks:office",
    "ADMIN": "tasks:office",
}


@login_required
def home(request):
    try:
        return redirect(reverse(ROLE_HOMES.get(request.user.role, "tasks:office")))
    except NoReverseMatch:
        return redirect("/admin/")
