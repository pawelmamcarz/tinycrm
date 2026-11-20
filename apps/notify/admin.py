from django.contrib import admin

from .models import DailyAlertLog, Notification, PushSubscription


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "created_at", "read_at")
    list_filter = ("read_at",)
    search_fields = ("title", "user__username")
    readonly_fields = ("created_at",)


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at")
    readonly_fields = ("endpoint", "p256dh", "auth", "created_at")


@admin.register(DailyAlertLog)
class DailyAlertLogAdmin(admin.ModelAdmin):
    list_display = ("key", "day", "created_at")
    readonly_fields = ("key", "day", "created_at")
