from .navigation import nav_for_user


def navigation(request):
    user = getattr(request, "user", None)
    return {"nav_items": nav_for_user(user) if user else []}
