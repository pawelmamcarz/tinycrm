from django.contrib import admin

from .models import SyncRun, SyncState


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("organization", "source", "cursor", "last_success_at")
    list_filter = ("organization", "source")


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "source", "status", "created", "updated", "skipped")
    list_filter = ("organization", "source", "status")
    readonly_fields = [f.name for f in SyncRun._meta.fields]

    def has_add_permission(self, request):
        return False
