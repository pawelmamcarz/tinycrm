"""Formularz zadania. Statusu tu NIE ma — zmienia się go przyciskami (FSM)."""
from django import forms

from apps.customers.models import Customer

from .models import Task
from .services import assignable_users

_INPUT = "input input-bordered w-full"
_SELECT = "select select-bordered w-full"


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ("title", "description", "kind", "priority", "customer", "assignee", "due_date")
        widgets = {
            "title": forms.TextInput(attrs={"class": _INPUT, "autofocus": True}),
            "description": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 3}),
            "kind": forms.Select(attrs={"class": _SELECT}),
            "priority": forms.Select(attrs={"class": _SELECT}),
            "customer": forms.Select(attrs={"class": _SELECT}),
            "assignee": forms.Select(attrs={"class": _SELECT}),
            # Natywny picker + ISO — polski locale nie wymaże wartości.
            "due_date": forms.DateInput(attrs={"class": _INPUT, "type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_date"].input_formats = ["%Y-%m-%d"]
        self.fields["customer"].queryset = Customer.objects.for_org(organization).filter(is_active=True)
        self.fields["customer"].empty_label = "— bez klienta —"
        self.fields["assignee"].queryset = assignable_users(organization)
        self.fields["assignee"].empty_label = "— automatycznie / nieprzypisane —"
        self.fields["assignee"].label_from_instance = lambda u: f"{u} · {u.get_role_display()}"
