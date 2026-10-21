from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        TEACHER = "teacher", "Teacher"
        STUDENT = "student", "Student / Parent"

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.STUDENT,
    )

class Batch(models.Model):
    name = models.CharField(max_length=100)
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="batches",
        limit_choices_to={"role": "teacher"},
    )
    start_date = models.DateField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Student(models.Model):
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, blank=True)
    guardian_name = models.CharField(max_length=150, blank=True)
    # The login account (student or parent) allowed to view this student's data.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name


class Enrollment(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="enrollments"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="enrollments"
    )
    joined_on = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "batch"], name="unique_student_batch"
            )
        ]

    def __str__(self):
        return f"{self.student} in {self.batch}"