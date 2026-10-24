from rest_framework import generics, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User
from .selectors import batches_for, fee_plans_for, students_for
from .serializers import BatchSerializer, DuesRowSerializer, StudentSerializer
from .services import dues_report


class HasFeeAccess(permissions.BasePermission):
    """Owners and students/parents may see fees. Teachers may not."""

    message = "You do not have access to fee information."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (
            User.Role.OWNER,
            User.Role.STUDENT,
        )


class MeView(APIView):
    def get(self, request):
        user = request.user
        return Response(
            {"id": user.id, "username": user.username, "role": user.role}
        )


class BatchViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BatchSerializer

    def get_queryset(self):
        return batches_for(self.request.user).select_related("teacher").order_by("name")


class StudentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StudentSerializer

    def get_queryset(self):
        return students_for(self.request.user).order_by("full_name")


class DuesView(generics.ListAPIView):
    serializer_class = DuesRowSerializer
    permission_classes = [permissions.IsAuthenticated, HasFeeAccess]

    def get_queryset(self):
        return dues_report(fee_plans=fee_plans_for(self.request.user))