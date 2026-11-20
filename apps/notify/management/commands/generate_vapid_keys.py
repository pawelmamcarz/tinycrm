"""Generuje nową parę kluczy VAPID (web push) do wklejenia w `.env`. Nic nie zapisuje na dysku."""
from django.core.management.base import BaseCommand


def generate_vapid_keypair():
    """Zwraca (public, private) w base64url: publiczny = punkt nieskompresowany (65 B), prywatny = surowe 32 B."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    from py_vapid.utils import b64urlencode

    vapid = Vapid()
    vapid.generate_keys()
    public_raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return b64urlencode(public_raw), b64urlencode(private_raw)


class Command(BaseCommand):
    help = "Wypisuje nową parę kluczy VAPID dla web push (do pliku .env)."

    def handle(self, *args, **options):
        public, private = generate_vapid_keypair()
        self.stdout.write("# Wklej do .env (klucz prywatny trzymaj w tajemnicy, nie commituj):")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write("VAPID_SUBJECT=mailto:admin@example.com")
