from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Batch, Enrollment, Student, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Role", {"fields": ("role",)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "email", "role", "is_staff")

admin.site.register(Batch)
admin.site.register(Student)
admin.site.register(Enrollment)