from django.contrib import admin
from django.db import transaction

from . import services

from .models import Device, Inspection, PartOrder, ServiceTicket


class InspectionInline(admin.TabularInline):
    model = Inspection
    extra = 0
    fields = ("due_date", "scheduled_for", "status", "performed_at", "performed_by", "protocol")
    # Status i wykonanie zmieniają się przez serwisy (FSM + aktualizacja terminów urządzenia).
    readonly_fields = ("status", "performed_at", "performed_by", "protocol")
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class PartOrderInline(admin.TabularInline):
    model = PartOrder
    extra = 0
    fields = ("part_name", "part_number", "quantity", "supplier", "status", "expected_at")
    readonly_fields = ("status",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "serial_number", "customer", "organization", "next_inspection_due", "warranty_until")
    list_filter = ("organization",)
    search_fields = ("name", "serial_number", "manufacturer", "customer__name")
    raw_id_fields = ("customer",)
    inlines = [InspectionInline]


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("device", "organization", "due_date", "scheduled_for", "status", "performed_at")
    list_filter = ("organization", "status")
    search_fields = ("device__name", "device__serial_number", "device__customer__name")
    raw_id_fields = ("device", "performed_by")
    readonly_fields = ("status", "performed_at", "performed_by", "reminder_task_created", "created_at",
                       "updated_at")


@admin.register(ServiceTicket)
class ServiceTicketAdmin(admin.ModelAdmin):
    list_display = ("number", "title", "organization", "customer", "status", "assignee", "created_at")
    list_filter = ("organization", "status", "is_warranty")
    search_fields = ("number", "title", "description", "customer__name")
    raw_id_fields = ("customer", "device", "reported_by", "assignee")
    readonly_fields = ("number", "status", "created_at", "updated_at")
    inlines = [PartOrderInline]

    def save_model(self, request, obj, form, change):
        # Numer nadajemy tą samą ścieżką co `services.create_ticket` (blokada organizacji, int suffix).
        if not obj.number:
            with transaction.atomic():
                services.assign_ticket_number(obj)
                super().save_model(request, obj, form, change)
            return
        super().save_model(request, obj, form, change)


@admin.register(PartOrder)
class PartOrderAdmin(admin.ModelAdmin):
    list_display = ("part_name", "quantity", "organization", "customer", "ticket", "status", "expected_at")
    list_filter = ("organization", "status")
    search_fields = ("part_name", "part_number", "supplier", "customer__name")
    raw_id_fields = ("customer", "ticket")
    readonly_fields = ("status", "ordered_at", "delivered_at", "installed_at", "created_at", "updated_at")
