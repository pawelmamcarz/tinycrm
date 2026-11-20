class OrganizationMiddleware:
    """Ustawia `request.organization` na organizację zalogowanego użytkownika (albo None)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        request.organization = getattr(user, "organization", None) if user and user.is_authenticated else None
        return self.get_response(request)
