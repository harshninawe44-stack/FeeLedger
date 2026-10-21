from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Batch, Enrollment, FeePlan, Installment, LedgerEntry, Student, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Role", {"fields": ("role",)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "email", "role", "is_staff")

admin.site.register(Batch)
admin.site.register(Student)
admin.site.register(Enrollment)

class InstallmentInline(admin.TabularInline):
    model = Installment
    extra = 1


@admin.register(FeePlan)
class FeePlanAdmin(admin.ModelAdmin):
    inlines = [InstallmentInline]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "fee_plan", "kind", "amount", "paid_on", "received_by")

    # The ledger is append-only: rows can be added but never changed or deleted.
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False