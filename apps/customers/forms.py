import re

from django import forms
from django.contrib.auth import get_user_model

from .models import Contact, Customer

_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def normalize_nip(value):
    return re.sub(r"[^0-9]", "", value or "")


def nip_is_valid(nip):
    if len(nip) != 10 or not nip.isdigit():
        return False
    checksum = sum(int(d) * w for d, w in zip(nip[:9], _NIP_WEIGHTS)) % 11
    return checksum != 10 and checksum == int(nip[9])


class DaisyMixin:
    def _style(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "select select-bordered w-full")
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", "textarea textarea-bordered w-full")
                widget.attrs.setdefault("rows", 3)
            else:
                widget.attrs.setdefault("class", "input input-bordered w-full")


class CustomerForm(DaisyMixin, forms.ModelForm):
    class Meta:
        model = Customer
        fields = ("name", "short_name", "nip", "city", "address", "phone", "email", "email_domain", "owner",
                  "enova_id", "notes", "is_active")

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        users = get_user_model().objects.filter(organization=organization, is_active=True) if organization \
            else get_user_model().objects.none()
        self.fields["owner"].queryset = users.order_by("first_name", "last_name", "username")
        self._style()

    def clean_nip(self):
        raw = self.cleaned_data.get("nip", "")
        if not raw.strip():
            return ""
        nip = normalize_nip(raw)
        if not nip_is_valid(nip):
            raise forms.ValidationError("Nieprawidłowy NIP (10 cyfr z poprawną sumą kontrolną).")
        return nip

    def clean_email_domain(self):
        value = (self.cleaned_data.get("email_domain") or "").strip().lower()
        return value.removeprefix("@")

    def clean_enova_id(self):
        value = (self.cleaned_data.get("enova_id") or "").strip()
        if value and self.organization is not None:
            qs = Customer.objects.for_org(self.organization).filter(enova_id=value)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("Klient z tym ID enova już istnieje.")
        return value


class ContactForm(DaisyMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ("name", "position", "phone", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
