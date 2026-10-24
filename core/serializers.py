from decimal import Decimal

from rest_framework import serializers

from .models import AttendanceRecord, Batch, LedgerEntry, Student


class BatchSerializer(serializers.ModelSerializer):
    teacher_name = serializers.CharField(source="teacher.username", read_only=True)

    class Meta:
        model = Batch
        fields = ["id", "name", "teacher", "teacher_name", "start_date", "is_active"]


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

    class Meta:
        model = LedgerEntry
        fields = [
            "id", "fee_plan", "student", "kind", "amount", "method",
            "reference", "note", "paid_on", "reverses",
            "received_by", "received_by_name", "created_at",
        ]
        read_only_fields = fields


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