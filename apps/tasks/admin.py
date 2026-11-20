from django.contrib import admin

from .models import Task, TaskComment


class TaskCommentInline(admin.TabularInline):
    model = TaskComment
    extra = 0
    fields = ("author", "text", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "status", "kind", "source", "customer", "assignee", "due_date")
    list_filter = ("organization", "status", "kind", "source")
    search_fields = ("title", "description", "source_ref")
    raw_id_fields = ("customer", "assignee", "created_by")
    # Status zmienia się przyciskami FSM w aplikacji — w adminie tylko do odczytu.
    readonly_fields = ("status", "done_at", "last_reminded_at", "escalated_at", "created_at", "updated_at")
    inlines = [TaskCommentInline]

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            obj.organization_id = form.instance.organization_id
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()
