"""Formularze logowania z komunikatem o blokadzie prób (patrz `throttle.py`)."""
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .throttle import THROTTLED_MESSAGE


class _ThrottleMessageMixin:
    def clean(self):
        try:
            return super().clean()
        except ValidationError:
            if getattr(self.request, "login_throttled", False):
                raise ValidationError(THROTTLED_MESSAGE, code="throttled") from None
            raise


class ThrottledAuthenticationForm(_ThrottleMessageMixin, AuthenticationForm):
    pass


class ThrottledAdminAuthenticationForm(_ThrottleMessageMixin, AdminAuthenticationForm):
    pass
