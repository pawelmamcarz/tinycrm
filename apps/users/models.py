from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    HANDLOWIEC = "HANDLOWIEC", "Handlowiec"
    BIURO = "BIURO", "Biuro obsługi"
    SERWIS = "SERWIS", "Serwis"
    SZEF = "SZEF", "Szef / kierownik"
    ADMIN = "ADMIN", "Administrator"


class User(AbstractUser):
    organization = models.ForeignKey(
        "core.Organization", null=True, blank=True, on_delete=models.PROTECT,
        related_name="users", verbose_name="organizacja",
    )
    role = models.CharField("rola", max_length=20, choices=Role.choices, default=Role.HANDLOWIEC)
    phone = models.CharField("telefon", max_length=30, blank=True)

    class Meta:
        verbose_name = "użytkownik"
        verbose_name_plural = "użytkownicy"

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def is_office(self):
        return self.role in (Role.BIURO, Role.SZEF, Role.ADMIN)
