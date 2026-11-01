from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    AttendanceChange,
    AttendanceRecord,
    AttendanceSession,
    Batch,
    Enrollment,
    FeePlan,
    Installment,
    LedgerEntry,
    Student,
    User,
)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Role", {"fields": ("role",)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "email", "role", "is_staff")


admin.site.register(Batch)
admin.site.register(Student)
admin.site.register(Enrollment)


class ViewOnlyAdmin(admin.ModelAdmin):
    """Rows can be looked at here, never added, changed or deleted."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ViewOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class InstallmentInline(ViewOnlyInline):
    model = Installment


class AttendanceRecordInline(ViewOnlyInline):
    model = AttendanceRecord


@admin.register(FeePlan)
class FeePlanAdmin(ViewOnlyAdmin):
    list_display = ("id", "enrollment", "total_amount", "created_at")
    list_select_related = ("enrollment__student", "enrollment__batch")
    inlines = [InstallmentInline]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ViewOnlyAdmin):
    list_display = ("id", "fee_plan", "kind", "amount", "paid_on", "received_by")
    list_select_related = (
        "fee_plan__enrollment__student",
        "fee_plan__enrollment__batch",
        "received_by",
    )


@admin.register(AttendanceSession)
class AttendanceSessionAdmin(ViewOnlyAdmin):
    list_display = ("batch", "date", "created_by")
    list_select_related = ("batch", "created_by")
    inlines = [AttendanceRecordInline]


@admin.register(AttendanceChange)
class AttendanceChangeAdmin(ViewOnlyAdmin):
    list_display = ("record", "old_status", "new_status", "changed_by", "changed_at")
    list_select_related = (
        "record__student",
        "record__session__batch",
        "changed_by",
    )