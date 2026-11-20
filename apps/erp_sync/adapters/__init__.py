"""Adaptery źródeł kontrahentów (wzorzec DataSync): CSV i enova365 WebAPI."""
from .base import CustomerRecord, CustomerSource, Page, normalize_nip

__all__ = ["CustomerRecord", "CustomerSource", "Page", "normalize_nip"]
