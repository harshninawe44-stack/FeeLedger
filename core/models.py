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