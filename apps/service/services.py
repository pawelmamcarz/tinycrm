"""Publiczne API modułu serwisu. Inne moduły (np. voice) tworzą zgłoszenia i zamówienia części tędy."""
from django.db import transaction
from django.utils import timezone

from .models import PartOrder, ServiceTicket


def _next_ticket_number(organization):
    year = timezone.localdate().year
    prefix = f"SRV-{year}-"
    numbers = ServiceTicket.objects.for_org(organization).filter(number__startswith=prefix).values_list("number",
                                                                                                         flat=True)
    # Sortuj po int suffix, nie po stringu (9999 < 10000).
    last = max((int(n.rsplit("-", 1)[1]) for n in numbers if n.rsplit("-", 1)[1].isascii()
                and n.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{prefix}{last + 1:04d}"


@transaction.atomic
def create_ticket(organization, *, customer, title, description="", device=None, reported_by=None,
                  is_warranty=False, source_ref=""):
    ticket = ServiceTicket.objects.create(
        organization=organization, number=_next_ticket_number(organization), customer=customer, device=device,
        title=title[:255], description=description, reported_by=reported_by, is_warranty=is_warranty,
        source_ref=source_ref,
    )
    return ticket


def create_part_order(organization, *, customer, part_name, quantity=1, ticket=None, part_number="", supplier=""):
    return PartOrder.objects.create(
        organization=organization, customer=customer, ticket=ticket, part_name=part_name[:200],
        quantity=quantity, part_number=part_number, supplier=supplier,
    )
