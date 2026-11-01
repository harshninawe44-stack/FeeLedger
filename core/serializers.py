import re
from decimal import Decimal

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .importers import PHONE_PATTERN
from .models import (
    AttendanceRecord,
    Batch,
    Enrollment,
    FeePlan,
    Installment,
    LedgerEntry,
    Student,
    User,
)
from .services import create_student

class BatchSerializer(serializers.ModelSerializer):
    # Only an active teacher can be assigned to a batch.
    teacher = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role=User.Role.TEACHER, is_active=True)
    )
    teacher_name = serializers.CharField(source="teacher.username", read_only=True)

    class Meta:
        model = Batch
        fields = ["id", "name", "teacher", "teacher_name", "start_date", "is_active"]

    def validate_name(self, value):
        value = value.strip()
        # The CSV importer finds batches by name, so names must not clash,
        # even when they differ only in capital letters.
        clash = Batch.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("A batch with this name already exists.")
        return value


class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = ["id", "full_name", "phone", "guardian_name", "is_active"]


class DuesRowSerializer(serializers.Serializer):
    fee_plan_id = serializers.IntegerField(source="pk")
    student_id = serializers.IntegerField(source="enrollment.student_id")
    student = serializers.CharField(source="enrollment.student.full_name")
    batch = serializers.CharField(source="enrollment.batch.name")
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    paid = serializers.DecimalField(max_digits=12, decimal_places=2)
    due_so_far = serializers.DecimalField(max_digits=12, decimal_places=2)
    overdue = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance = serializers.DecimalField(max_digits=12, decimal_places=2)


# ---- Payments -------------------------------------------------------------


class LedgerEntrySerializer(serializers.ModelSerializer):
    """What the API SHOWS for a ledger entry. Everything is read-only."""

    student = serializers.CharField(
        source="fee_plan.enrollment.student.full_name", read_only=True
    )
    received_by_name = serializers.CharField(
        source="received_by.username", read_only=True
    )
    is_reversed = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = [
            "id", "fee_plan", "student", "kind", "amount", "method",
            "reference", "note", "paid_on", "reverses", "is_reversed",
            "received_by", "received_by_name", "created_at",
        ]
        read_only_fields = fields

    def get_is_reversed(self, obj):
        # The payment list marks every row in one query. A freshly created
        # entry has no mark yet, so for that one row we ask the database.
        flagged = getattr(obj, "is_reversed", None)
        if flagged is not None:
            return flagged
        return (
            obj.kind == LedgerEntry.Kind.PAYMENT
            and LedgerEntry.objects.filter(reverses=obj).exists()
        )

class PaymentInputSerializer(serializers.Serializer):
    """What the API ACCEPTS when recording a payment."""

    fee_plan = serializers.IntegerField()
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    method = serializers.ChoiceField(choices=LedgerEntry.Method.choices)
    paid_on = serializers.DateField()
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class ReverseInputSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


# ---- Attendance -----------------------------------------------------------


class AttendanceRecordInputSerializer(serializers.Serializer):
    student = serializers.IntegerField()
    status = serializers.ChoiceField(choices=AttendanceRecord.Status.choices)


class AttendanceSheetInputSerializer(serializers.Serializer):
    date = serializers.DateField()
    records = AttendanceRecordInputSerializer(many=True)

# ---- Fee plans ------------------------------------------------------------


class InstallmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Installment
        fields = ["number", "amount", "due_date"]
        read_only_fields = fields


class FeePlanSerializer(serializers.ModelSerializer):
    student = serializers.CharField(
        source="enrollment.student.full_name", read_only=True
    )
    batch = serializers.CharField(source="enrollment.batch.name", read_only=True)
    installments = InstallmentSerializer(many=True, read_only=True)

    class Meta:
        model = FeePlan
        fields = [
            "id", "enrollment", "student", "batch",
            "total_amount", "installments", "created_at",
        ]
        read_only_fields = fields


class FeePlanInputSerializer(serializers.Serializer):
    enrollment = serializers.IntegerField()
    total_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    due_dates = serializers.ListField(
        child=serializers.DateField(), min_length=1, max_length=24
    )

# ---- CSV import -----------------------------------------------------------


class ImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    commit = serializers.BooleanField(default=False)

# ---- Login ----------------------------------------------------------------


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    # trim_whitespace=False: spaces at the ends of a password are part of it.
    password = serializers.CharField(max_length=128, trim_whitespace=False)

    def get_is_reversed(self, obj):
        # The payment list marks every row in one query. A freshly created
        # entry has no mark yet, so for that one row we ask the database.
        flagged = getattr(obj, "is_reversed", None)
        if flagged is not None:
            return flagged
        return (
            obj.kind == LedgerEntry.Kind.PAYMENT
            and LedgerEntry.objects.filter(reverses=obj).exists()
        )

# ---- Owner screens --------------------------------------------------------


class EnrollmentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True)
    has_fee_plan = serializers.BooleanField(read_only=True)

    class Meta:
        model = Enrollment
        fields = [
            "id", "student", "student_name", "batch", "batch_name",
            "joined_on", "is_active", "has_fee_plan",
        ]
        read_only_fields = fields


class FeePlanPreviewInputSerializer(serializers.Serializer):
    total_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    due_dates = serializers.ListField(
        child=serializers.DateField(), min_length=1, max_length=24
    )


class InstallmentPreviewSerializer(serializers.Serializer):
    number = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    due_date = serializers.DateField()

# ---- Attendance (reading) -------------------------------------------------


class AttendanceRecordSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)
    batch = serializers.IntegerField(source="session.batch_id", read_only=True)
    batch_name = serializers.CharField(source="session.batch.name", read_only=True)
    date = serializers.DateField(source="session.date", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = [
            "id", "student", "student_name", "batch", "batch_name", "date", "status",
        ]
        read_only_fields = fields

# ---- Accounts (owner screens) ---------------------------------------------


class UserSerializer(serializers.ModelSerializer):
    """What the API SHOWS for an account. Never includes the password."""

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "role", "is_active"]
        read_only_fields = fields


class UserCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["username", "first_name", "role", "password"]
        extra_kwargs = {
            # write_only: accepted when creating, never sent back.
            # trim_whitespace=False: spaces at the ends are part of a password.
            "password": {
                "write_only": True,
                "trim_whitespace": False,
                "style": {"input_type": "password"},
            },
            "role": {"required": True},
        }

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate_role(self, value):
        if value not in (User.Role.TEACHER, User.Role.STUDENT):
            raise serializers.ValidationError(
                "Only teacher and parent accounts can be created here."
            )
        return value

    def validate(self, attrs):
        # Django's own password rules (length, common passwords, similarity
        # to the username). They are listed in AUTH_PASSWORD_VALIDATORS.
        candidate = User(
            username=attrs["username"], first_name=attrs.get("first_name", "")
        )
        try:
            password_validation.validate_password(attrs["password"], candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": list(error.messages)})
        return attrs

    def create(self, validated_data):
        # create_user hashes the password. Plain create() would store it as text.
        return User.objects.create_user(**validated_data)

# ---- Students and enrolments (owner screens) -------------------------------


class EnrollmentBriefSerializer(serializers.ModelSerializer):
    """One enrolment, shown inside a student."""

    batch_name = serializers.CharField(source="batch.name", read_only=True)

    class Meta:
        model = Enrollment
        fields = ["id", "batch", "batch_name", "joined_on", "is_active"]
        read_only_fields = fields


def clean_phone(value):
    """Drop spaces, dashes and brackets, the same way the CSV importer does."""
    return re.sub(r"[\s\-()]", "", value)


class StudentRulesMixin:
    """Rules shared by adding and editing a student."""

    def validate_phone(self, value):
        value = clean_phone(value)
        if not PHONE_PATTERN.match(value):
            raise serializers.ValidationError(
                "Phone must be 10 to 13 digits, optionally starting with +."
            )
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = getattr(self, "instance", None)
        # Only look for a clash when the name or phone is being set or changed,
        # so old data can never block an edit of something else.
        if instance is None or "full_name" in attrs or "phone" in attrs:
            name = attrs.get("full_name", instance.full_name if instance else "")
            phone = attrs.get("phone", instance.phone if instance else "")
            clash = Student.objects.filter(full_name__iexact=name, phone=phone)
            if instance is not None:
                clash = clash.exclude(pk=instance.pk)
            if phone and clash.exists():
                raise serializers.ValidationError(
                    "A student with this name and phone already exists."
                )
        return attrs


class StudentAdminSerializer(StudentRulesMixin, serializers.ModelSerializer):
    """What the owner sees when listing students, and what an edit accepts."""

    phone = serializers.CharField(max_length=20, required=False)
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role=User.Role.STUDENT, is_active=True),
        required=False,
        allow_null=True,
    )
    parent_name = serializers.SerializerMethodField()
    enrollments = EnrollmentBriefSerializer(many=True, read_only=True)

    class Meta:
        model = Student
        fields = [
            "id", "full_name", "phone", "guardian_name", "user",
            "parent_name", "is_active", "enrollments",
        ]

    def get_parent_name(self, obj):
        return obj.user.username if obj.user_id else None

    @transaction.atomic
    def update(self, instance, validated_data):
        was_active = instance.is_active
        student = super().update(instance, validated_data)
        if was_active and not student.is_active:
            # A student who has left no longer appears on any attendance sheet.
            student.enrollments.filter(is_active=True).update(is_active=False)
        return student


class StudentCreateSerializer(StudentRulesMixin, serializers.ModelSerializer):
    """What the owner sends to add a student, optionally enrolling them at once."""

    phone = serializers.CharField(max_length=20)
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role=User.Role.STUDENT, is_active=True),
        required=False,
        allow_null=True,
    )
    batch = serializers.PrimaryKeyRelatedField(
        queryset=Batch.objects.all(), required=False, allow_null=True, write_only=True
    )
    joined_on = serializers.DateField(required=False, write_only=True)

    class Meta:
        model = Student
        fields = ["full_name", "phone", "guardian_name", "user", "batch", "joined_on"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("joined_on") and not attrs.get("batch"):
            raise serializers.ValidationError("Choose a batch to set a joining date.")
        return attrs

    def create(self, validated_data):
        return create_student(**validated_data)


class EnrollmentCreateSerializer(serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(queryset=Student.objects.all())
    batch = serializers.PrimaryKeyRelatedField(queryset=Batch.objects.all())
    joined_on = serializers.DateField(required=False)


class EnrollmentUpdateSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()