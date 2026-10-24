from rest_framework import serializers

from .models import Batch, Student


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