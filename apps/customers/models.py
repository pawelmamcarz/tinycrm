from django.conf import settings
from django.db import models

from apps.core.models import OrgScopedModel


class Customer(OrgScopedModel):
    """Kontrahent (szpital, klinika, gabinet). `enova_id` = ID kontrahenta w enova365."""

    name = models.CharField("nazwa", max_length=255)
    short_name = models.CharField("nazwa skrócona", max_length=100, blank=True)
    nip = models.CharField("NIP", max_length=20, blank=True)
    city = models.CharField("miasto", max_length=100, blank=True)
    address = models.CharField("adres", max_length=255, blank=True)
    phone = models.CharField("telefon", max_length=50, blank=True)
    email = models.EmailField("e-mail", blank=True)
    email_domain = models.CharField("domena e-mail", max_length=100, blank=True,
                                    help_text="Do przypinania maili do klienta, np. szpital-radom.pl")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_customers", verbose_name="opiekun (handlowiec)")
    enova_id = models.CharField("ID w enova", max_length=50, blank=True)
    notes = models.TextField("notatki", blank=True)
    is_active = models.BooleanField("aktywny", default=True)

    class Meta:
        verbose_name = "klient"
        verbose_name_plural = "klienci"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "enova_id"], condition=~models.Q(enova_id=""),
                                    name="uniq_customer_enova_id_per_org"),
        ]

    def __str__(self):
        return self.short_name or self.name


class Contact(OrgScopedModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts", verbose_name="klient")
    name = models.CharField("imię i nazwisko", max_length=200)
    position = models.CharField("stanowisko / oddział", max_length=200, blank=True)
    phone = models.CharField("telefon", max_length=50, blank=True)
    email = models.EmailField("e-mail", blank=True)

    class Meta:
        verbose_name = "osoba kontaktowa"
        verbose_name_plural = "osoby kontaktowe"
        ordering = ["name"]

    def __str__(self):
        return self.name
