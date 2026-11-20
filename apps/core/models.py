"""Wspólne bazy modeli: organizacja (furtka na SaaS), znaczniki czasu, zakres organizacji."""
from django.db import models


class Organization(models.Model):
    """Firma-klient TinyCRM (np. dystrybutor). Każdy rekord domenowy należy do organizacji."""

    name = models.CharField("nazwa", max_length=200)
    slug = models.SlugField("identyfikator", unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "organizacja"
        verbose_name_plural = "organizacje"

    def __str__(self):
        return self.name


class TimestampedModel(models.Model):
    created_at = models.DateTimeField("utworzono", auto_now_add=True)
    updated_at = models.DateTimeField("zmieniono", auto_now=True)

    class Meta:
        abstract = True


class OrgQuerySet(models.QuerySet):
    def for_org(self, organization):
        """Zawsze filtruj po organizacji. Brak organizacji = pusty wynik (fail-closed)."""
        if organization is None:
            return self.none()
        return self.filter(organization=organization)


class OrgScopedModel(TimestampedModel):
    """Baza dla modeli domenowych: FK do organizacji + manager z `for_org`."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="+", verbose_name="organizacja"
    )

    objects = OrgQuerySet.as_manager()

    class Meta:
        abstract = True
