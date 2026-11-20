"""Formularze serwisu (DaisyUI, natywne pola daty w formacie ISO)."""
import os

from django import forms
from django.utils import timezone

from apps.customers.models import Customer
from apps.users.models import Role, User

from .models import Device, PartOrder, ServiceTicket

PROTOCOL_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
PROTOCOL_MAX_BYTES = 20 * 1024 * 1024
SERVICE_STAFF_ROLES = (Role.SERWIS, Role.BIURO, Role.SZEF, Role.ADMIN)


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%d")


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
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs.setdefault("class", "file-input file-input-bordered w-full")
            else:
                widget.attrs.setdefault("class", "input input-bordered w-full")


def customers_for(user, organization):
    qs = Customer.objects.for_org(organization).filter(is_active=True)
    if user.role == Role.HANDLOWIEC:
        qs = qs.filter(owner=user)
    return qs.order_by("name")


def service_staff(organization):
    return User.objects.filter(organization=organization, is_active=True,
                               role__in=SERVICE_STAFF_ROLES).order_by("first_name", "username")


class DeviceForm(DaisyMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ("customer", "name", "manufacturer", "serial_number", "location", "installed_at", "warranty_until",
                  "inspection_interval_months", "last_inspection_at", "next_inspection_due")
        widgets = {f: DateInput() for f in ("installed_at", "warranty_until", "last_inspection_at",
                                            "next_inspection_due")}
        help_texts = {"next_inspection_due": "Puste = wyliczy się z ostatniego przeglądu albo daty instalacji."}

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.for_org(organization).order_by("name")
        self.fields["inspection_interval_months"].min_value = 1
        self._style()

    def clean_inspection_interval_months(self):
        value = self.cleaned_data["inspection_interval_months"]
        if not value or value < 1 or value > 120:
            raise forms.ValidationError("Podaj od 1 do 120 miesięcy.")
        return value


class TicketForm(DaisyMixin, forms.ModelForm):
    class Meta:
        model = ServiceTicket
        fields = ("customer", "device", "title", "description", "is_warranty", "assignee")

    def __init__(self, *args, user=None, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        customers = customers_for(user, organization)
        self.fields["customer"].queryset = customers
        self.fields["device"].queryset = Device.objects.for_org(organization).filter(customer__in=customers)
        self.fields["device"].required = False
        if user.role == Role.HANDLOWIEC:
            # Handlowiec tylko zgłasza — przydział robi serwis/biuro.
            del self.fields["assignee"]
        else:
            self.fields["assignee"].queryset = service_staff(organization)
            self.fields["assignee"].required = False
        self._style()

    def clean(self):
        cleaned = super().clean()
        customer, device = cleaned.get("customer"), cleaned.get("device")
        if customer and device and device.customer_id != customer.pk:
            self.add_error("device", "To urządzenie należy do innego klienta.")
        return cleaned


class PartOrderForm(DaisyMixin, forms.ModelForm):
    class Meta:
        model = PartOrder
        fields = ("part_name", "part_number", "quantity", "supplier", "expected_at")
        widgets = {"expected_at": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].min_value = 1
        self._style()

    def clean_quantity(self):
        value = self.cleaned_data["quantity"]
        if not value or value < 1:
            raise forms.ValidationError("Ilość musi być co najmniej 1.")
        return value


class CompleteInspectionForm(DaisyMixin, forms.Form):
    performed_at = forms.DateField(label="Data wykonania", widget=DateInput(), input_formats=["%Y-%m-%d"])
    protocol = forms.FileField(label="Protokół (PDF / zdjęcie)", required=False)
    notes = forms.CharField(label="Uwagi", required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["performed_at"].initial = timezone.localdate()
        self._style()

    def clean_performed_at(self):
        value = self.cleaned_data["performed_at"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data nie może być z przyszłości.")
        return value

    def clean_protocol(self):
        upload = self.cleaned_data.get("protocol")
        if not upload:
            return upload
        ext = os.path.splitext(upload.name)[1].lower()
        if ext not in PROTOCOL_EXTENSIONS:
            raise forms.ValidationError("Dozwolone pliki: PDF, JPG, PNG.")
        if upload.size > PROTOCOL_MAX_BYTES:
            raise forms.ValidationError("Plik jest za duży (max 20 MB).")
        return upload
