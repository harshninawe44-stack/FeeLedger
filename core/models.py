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

class FeePlan(models.Model):
    enrollment = models.OneToOneField(
        Enrollment, on_delete=models.PROTECT, related_name="fee_plan"
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gt=0),
                name="feeplan_total_positive",
            )
        ]

    def __str__(self):
        return f"Fee plan for {self.enrollment}"


class Installment(models.Model):
    fee_plan = models.ForeignKey(
        FeePlan, on_delete=models.CASCADE, related_name="installments"
    )
    number = models.PositiveSmallIntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_date = models.DateField()

    class Meta:
        ordering = ["fee_plan", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["fee_plan", "number"], name="unique_installment_number"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="installment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"Installment {self.number} of {self.fee_plan}"


class LedgerEntry(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment", "Payment"
        REVERSAL = "reversal", "Reversal"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CHEQUE = "cheque", "Cheque"
        BANK = "bank", "Bank transfer"

    fee_plan = models.ForeignKey(
        FeePlan, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=255, blank=True)
    paid_on = models.DateField()
    reverses = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="payment", amount__gt=0)
                    | models.Q(kind="reversal", amount__lt=0)
                ),
                name="ledger_amount_sign_matches_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="payment", reverses__isnull=True)
                    | models.Q(kind="reversal", reverses__isnull=False)
                ),
                name="ledger_reverses_matches_kind",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount} on {self.paid_on}"