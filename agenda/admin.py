from django.contrib import admin
from .models import Memory, Plan


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "status", "date", "location")
    list_filter = ("status", "category")
    search_fields = ("title", "location", "description")


@admin.register(Memory)
class MemoryAdmin(admin.ModelAdmin):
    list_display = ("plan_title", "happened_at", "note")
    search_fields = ("plan_title", "note")
