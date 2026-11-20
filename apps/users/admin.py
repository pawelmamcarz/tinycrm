from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class TinyUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("TinyCRM", {"fields": ("organization", "role", "phone")}),)
    list_display = ("username", "first_name", "last_name", "role", "organization")
