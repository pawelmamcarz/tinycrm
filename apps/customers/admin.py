from django.contrib import admin

from .models import Contact, Customer


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 0
    fields = ("name", "position", "phone", "email")



@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "city", "nip", "owner", "organization", "is_active")
    list_filter = ("organization", "is_active", "city")
    search_fields = ("name", "short_name", "city", "nip", "enova_id")
    raw_id_fields = ("owner",)
    inlines = [ContactInline]

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, Contact):
                obj.organization_id = form.instance.organization_id
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "position", "customer", "phone", "email", "organization")
    list_filter = ("organization",)
    search_fields = ("name", "position", "customer__name", "email")
    raw_id_fields = ("customer",)

    def save_model(self, request, obj, form, change):
        obj.organization_id = obj.customer.organization_id
        super().save_model(request, obj, form, change)
