"""Bramki ról. Używaj `role_required` na widokach funkcyjnych i `RoleRequiredMixin` na CBV."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied


def user_has_role(user, roles):
    if not user.is_authenticated or user.organization_id is None:
        return False
    return not roles or user.role in roles


def role_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not user_has_role(request.user, roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


class RoleRequiredMixin(LoginRequiredMixin):
    required_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not user_has_role(request.user, self.required_roles):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
