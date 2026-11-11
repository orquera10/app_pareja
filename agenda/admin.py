from django.contrib import admin
from .models import CoupleProfile, Memory, Plan


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "status", "date", "location", "created_by")
    list_filter = ("status", "category")
    search_fields = ("title", "location", "description")


@admin.register(Memory)
class MemoryAdmin(admin.ModelAdmin):
    list_display = ("plan_title", "happened_at", "note", "created_by")
    search_fields = ("plan_title", "note")


@admin.register(CoupleProfile)
class CoupleProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "partner", "onboarding_completed", "anniversary_date")
    search_fields = ("user__username", "display_name", "nickname_for_partner")
