from django.contrib import admin

from .models import VoiceNote


@admin.register(VoiceNote)
class VoiceNoteAdmin(admin.ModelAdmin):
    list_display = ("pk", "organization", "author", "customer", "status", "created_at", "confirmed_at")
    list_filter = ("status", "organization")
    search_fields = ("transcript", "typed_text")
    # Status zmieniają wyłącznie serwisy (FSM) — w adminie tylko do odczytu.
    readonly_fields = ("status", "extraction", "confirmed_at", "audio_deleted_at", "client_uuid", "error")
    raw_id_fields = ("author", "customer")
