from django.conf import settings
from django.db import models

from apps.core.models import OrgScopedModel


class Device(OrgScopedModel):
    """Urządzenie medyczne zainstalowane u klienta."""

    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="devices",
                                 verbose_name="klient")
    name = models.CharField("model / nazwa", max_length=200)
    manufacturer = models.CharField("producent", max_length=100, blank=True)
    serial_number = models.CharField("numer seryjny", max_length=100, blank=True)
    location = models.CharField("lokalizacja (oddział, sala)", max_length=200, blank=True)
    installed_at = models.DateField("data instalacji", null=True, blank=True)
    warranty_until = models.DateField("gwarancja do", null=True, blank=True)
    inspection_interval_months = models.PositiveSmallIntegerField("przegląd co (miesięcy)", default=12)
    last_inspection_at = models.DateField("ostatni przegląd", null=True, blank=True)
    next_inspection_due = models.DateField("następny przegląd", null=True, blank=True)

    class Meta:
        verbose_name = "urządzenie"
        verbose_name_plural = "urządzenia"
        ordering = ["next_inspection_due", "name"]

    def __str__(self):
        return f"{self.name} ({self.serial_number})" if self.serial_number else self.name


class InspectionStatus(models.TextChoices):
    ZAPLANOWANY = "ZAPLANOWANY", "Zaplanowany"
    UMOWIONY = "UMOWIONY", "Umówiony z klientem"
    WYKONANY = "WYKONANY", "Wykonany"
    ANULOWANY = "ANULOWANY", "Anulowany"


INSPECTION_LEGAL_TRANSITIONS = {
    InspectionStatus.ZAPLANOWANY: {InspectionStatus.UMOWIONY, InspectionStatus.WYKONANY, InspectionStatus.ANULOWANY},
    InspectionStatus.UMOWIONY: {InspectionStatus.WYKONANY, InspectionStatus.ZAPLANOWANY, InspectionStatus.ANULOWANY},
    InspectionStatus.WYKONANY: set(),
    InspectionStatus.ANULOWANY: set(),
}


class Inspection(OrgScopedModel):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="inspections", verbose_name="urządzenie")
    due_date = models.DateField("termin wymagany")
    scheduled_for = models.DateField("umówiony na", null=True, blank=True)
    status = models.CharField("status", max_length=20, choices=InspectionStatus.choices,
                              default=InspectionStatus.ZAPLANOWANY)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     verbose_name="wykonał")
    performed_at = models.DateField("wykonano", null=True, blank=True)
    protocol = models.FileField("protokół", upload_to="protokoly/%Y/", blank=True)
    notes = models.TextField("uwagi", blank=True)
    reminder_task_created = models.BooleanField(default=False)

    class Meta:
        verbose_name = "przegląd"
        verbose_name_plural = "przeglądy"
        ordering = ["due_date"]


class TicketStatus(models.TextChoices):
    NOWE = "NOWE", "Nowe"
    W_TOKU = "W_TOKU", "W toku"
    CZEKA_NA_CZESC = "CZEKA_NA_CZESC", "Czeka na część"
    ZAKONCZONE = "ZAKONCZONE", "Zakończone"
    ANULOWANE = "ANULOWANE", "Anulowane"


TICKET_LEGAL_TRANSITIONS = {
    TicketStatus.NOWE: {TicketStatus.W_TOKU, TicketStatus.CZEKA_NA_CZESC, TicketStatus.ANULOWANE},
    TicketStatus.W_TOKU: {TicketStatus.CZEKA_NA_CZESC, TicketStatus.ZAKONCZONE},
    TicketStatus.CZEKA_NA_CZESC: {TicketStatus.W_TOKU, TicketStatus.ZAKONCZONE},
    TicketStatus.ZAKONCZONE: {TicketStatus.W_TOKU},
    TicketStatus.ANULOWANE: set(),
}


class ServiceTicket(OrgScopedModel):
    """Zgłoszenie serwisowe: awaria, naprawa, reklamacja."""

    number = models.CharField("numer", max_length=20, blank=True)  # SRV-YYYY-NNNN, sortuj po int suffix
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="service_tickets",
                                 verbose_name="klient")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
                               verbose_name="urządzenie")
    title = models.CharField("tytuł", max_length=255)
    description = models.TextField("opis", blank=True)
    status = models.CharField("status", max_length=20, choices=TicketStatus.choices, default=TicketStatus.NOWE)
    is_warranty = models.BooleanField("gwarancyjne", default=False)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="reported_tickets", verbose_name="zgłosił")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="service_tickets", verbose_name="serwisant")
    source_ref = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "zgłoszenie serwisowe"
        verbose_name_plural = "zgłoszenia serwisowe"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} {self.title}".strip()


class PartOrderStatus(models.TextChoices):
    DO_ZAMOWIENIA = "DO_ZAMOWIENIA", "Do zamówienia"
    ZAMOWIONE = "ZAMOWIONE", "Zamówione"
    DOSTARCZONE = "DOSTARCZONE", "Dostarczone"
    ZAMONTOWANE = "ZAMONTOWANE", "Zamontowane"
    ANULOWANE = "ANULOWANE", "Anulowane"


PART_ORDER_LEGAL_TRANSITIONS = {
    PartOrderStatus.DO_ZAMOWIENIA: {PartOrderStatus.ZAMOWIONE, PartOrderStatus.ANULOWANE},
    PartOrderStatus.ZAMOWIONE: {PartOrderStatus.DOSTARCZONE, PartOrderStatus.ANULOWANE},
    PartOrderStatus.DOSTARCZONE: {PartOrderStatus.ZAMONTOWANE},
    PartOrderStatus.ZAMONTOWANE: set(),
    PartOrderStatus.ANULOWANE: set(),
}


class PartOrder(OrgScopedModel):
    ticket = models.ForeignKey(ServiceTicket, null=True, blank=True, on_delete=models.CASCADE,
                               related_name="part_orders", verbose_name="zgłoszenie")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="part_orders",
                                 verbose_name="klient")
    part_name = models.CharField("część", max_length=200)
    part_number = models.CharField("nr katalogowy", max_length=100, blank=True)
    quantity = models.PositiveIntegerField("ilość", default=1)
    supplier = models.CharField("dostawca / producent", max_length=200, blank=True)
    status = models.CharField("status", max_length=20, choices=PartOrderStatus.choices,
                              default=PartOrderStatus.DO_ZAMOWIENIA)
    expected_at = models.DateField("spodziewana dostawa", null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    installed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "zamówienie części"
        verbose_name_plural = "zamówienia części"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.part_name} ×{self.quantity}"
