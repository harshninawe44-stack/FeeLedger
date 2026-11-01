from django.contrib.auth import authenticate, login, logout
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import generics, mixins, permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .importers import MAX_BYTES, ImportFileError, import_students
from .models import AttendanceRecord, Enrollment, FeePlan, LedgerEntry, User
from .selectors import (
    attendance_records_for,
    batches_for,
    enrollments_for,
    fee_plans_for,
    ledger_entries_for,
    students_for,
)
from .serializers import (
    AttendanceRecordSerializer,
    AttendanceSheetInputSerializer,
    BatchSerializer,
    DuesRowSerializer,
    EnrollmentSerializer,
    FeePlanInputSerializer,
    FeePlanPreviewInputSerializer,
    FeePlanSerializer,
    ImportUploadSerializer,
    InstallmentPreviewSerializer,
    LedgerEntrySerializer,
    LoginSerializer,
    PaymentInputSerializer,
    ReverseInputSerializer,
    StudentSerializer,
    UserCreateSerializer,
    UserSerializer,
)
from .services import (
    AttendanceError,
    LedgerError,
    create_fee_plan,
    dues_report,
    mark_attendance,
    preview_installments,
    record_payment,
    reverse_payment,
)

def _digits(value):
    """The value as a number if it is plain ASCII digits, otherwise None."""
    if value and len(value) <= 18 and value.isascii() and value.isdigit():
        return int(value)
    return None

def _optional_date(request, name):
    """A date from the query string, or None if it is missing. Bad text gives a 400."""
    value = request.query_params.get(name)
    if not value:
        return None
    return serializers.DateField().run_validation(value)

# ---- Permissions ----------------------------------------------------------


class HasFeeAccess(permissions.BasePermission):
    """Owners and students/parents may see fees. Teachers may not."""

    message = "You do not have access to fee information."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (
            User.Role.OWNER,
            User.Role.STUDENT,
        )


class IsOwner(permissions.BasePermission):
    message = "Only the owner can do this."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == User.Role.OWNER


class IsOwnerOrTeacher(permissions.BasePermission):
    message = "Only owners and teachers can do this."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (
            User.Role.OWNER,
            User.Role.TEACHER,
        )


# ---- Read-only endpoints --------------------------------------------------


class MeView(APIView):
    def get(self, request):
        user = request.user
        return Response({"id": user.id, "username": user.username, "role": user.role})


class StudentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StudentSerializer

    def get_queryset(self):
        return students_for(self.request.user).order_by("full_name")


class DuesView(generics.ListAPIView):
    serializer_class = DuesRowSerializer
    permission_classes = [permissions.IsAuthenticated, HasFeeAccess]

    def get_queryset(self):
        plans = fee_plans_for(self.request.user)
        fee_plan = _digits(self.request.query_params.get("fee_plan"))
        if fee_plan is not None:
            plans = plans.filter(pk=fee_plan)
        return dues_report(fee_plans=plans)


# ---- Attendance helpers ---------------------------------------------------


def _query_date(request):
    value = request.query_params.get("date")
    if not value:
        return timezone.localdate()
    return serializers.DateField().run_validation(value)  # 400 if malformed


def _attendance_sheet(batch, day):
    """The roster for one batch and date, with each student's status (or None)."""
    enrollments = (
        Enrollment.objects.filter(batch=batch, is_active=True)
        .select_related("student")
        .order_by("student__full_name")
    )
    statuses = dict(
        AttendanceRecord.objects.filter(
            session__batch=batch, session__date=day
        ).values_list("student_id", "status")
    )
    return {
        "batch": batch.pk,
        "date": day,
        "records": [
            {
                "student": e.student_id,
                "student_name": e.student.full_name,
                "status": statuses.get(e.student_id),
            }
            for e in enrollments
        ],
    }


class BatchViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Everyone reads their own batches. Only the owner creates or edits."""

    serializer_class = BatchSerializer
    # No PUT and no DELETE: batches are edited with PATCH or deactivated.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update"):
            return [permissions.IsAuthenticated(), IsOwner()]
        # The attendance action carries its own permission_classes.
        return super().get_permissions()

    def get_queryset(self):
        return batches_for(self.request.user).select_related("teacher").order_by("name")

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="attendance",
        permission_classes=[permissions.IsAuthenticated, IsOwnerOrTeacher],
        serializer_class=AttendanceSheetInputSerializer,
    )
    def attendance(self, request, pk=None):
        batch = self.get_object()  # scoped: 404 if this isn't the user's batch

        if request.method == "GET":
            return Response(_attendance_sheet(batch, _query_date(request)))

        body = AttendanceSheetInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        day = body.validated_data["date"]
        records = body.validated_data["records"]

        statuses = {r["student"]: r["status"] for r in records}
        if len(statuses) != len(records):
            raise ValidationError({"detail": "Each student can appear only once."})

        try:
            mark_attendance(
                batch=batch, date=day, statuses=statuses, marked_by=request.user
            )
        except AttendanceError as error:
            raise ValidationError({"detail": str(error)})
        return Response(_attendance_sheet(batch, day))

# ---- Payments -------------------------------------------------------------


class PaymentViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Read the ledger, record a payment, reverse a payment.
    There is no update or delete: the ledger is append-only."""

    def get_permissions(self):
        if self.action in ("create", "reverse"):
            return [permissions.IsAuthenticated(), IsOwner()]
        return [permissions.IsAuthenticated(), HasFeeAccess()]

    def get_serializer_class(self):
        if self.action == "create":
            return PaymentInputSerializer
        if self.action == "reverse":
            return ReverseInputSerializer
        return LedgerEntrySerializer

    def get_queryset(self):
        queryset = (
            ledger_entries_for(self.request.user)
            .select_related("fee_plan__enrollment__student", "received_by")
            .annotate(
                is_reversed=Exists(
                    LedgerEntry.objects.filter(reverses=OuterRef("pk"))
                )
            )
            .order_by("-paid_on", "-id")
        )
        fee_plan = _digits(self.request.query_params.get("fee_plan"))
        if fee_plan is not None:
            queryset = queryset.filter(fee_plan_id=fee_plan)
        return queryset

    def create(self, request, *args, **kwargs):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        plan = fee_plans_for(request.user).filter(pk=data["fee_plan"]).first()
        if plan is None:
            raise NotFound("Fee plan not found.")

        try:
            entry = record_payment(
                fee_plan=plan,
                amount=data["amount"],
                method=data["method"],
                paid_on=data["paid_on"],
                received_by=request.user,
                reference=data["reference"],
                note=data["note"],
            )
        except LedgerError as error:
            raise ValidationError({"detail": str(error)})
        return Response(LedgerEntrySerializer(entry).data, status=201)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        entry = self.get_object()  # scoped: 404 if the entry isn't visible to you
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            reversal = reverse_payment(
                entry=entry,
                reason=body.validated_data["reason"],
                received_by=request.user,
            )
        except LedgerError as error:
            raise ValidationError({"detail": str(error)})
        return Response(LedgerEntrySerializer(reversal).data, status=201)

# ---- Fee plans ------------------------------------------------------------


class FeePlanViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    def get_permissions(self):
        if self.action in ("create", "preview"):
            return [permissions.IsAuthenticated(), IsOwner()]
        return [permissions.IsAuthenticated(), HasFeeAccess()]

    def get_serializer_class(self):
        if self.action == "create":
            return FeePlanInputSerializer
        if self.action == "preview":
            return FeePlanPreviewInputSerializer
        return FeePlanSerializer

    def get_queryset(self):
        return (
            fee_plans_for(self.request.user)
            .select_related("enrollment__student", "enrollment__batch")
            .prefetch_related("installments")
            .order_by("-id")
        )

    def create(self, request, *args, **kwargs):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        enrollment = enrollments_for(request.user).filter(pk=data["enrollment"]).first()
        if enrollment is None:
            raise NotFound("Enrollment not found.")

        try:
            plan = create_fee_plan(
                enrollment=enrollment,
                total_amount=data["total_amount"],
                due_dates=data["due_dates"],
            )
        except LedgerError as error:
            raise ValidationError({"detail": str(error)})
        return Response(FeePlanSerializer(plan).data, status=201)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Show how a total would be split. Saves nothing."""
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            rows = preview_installments(
                total_amount=body.validated_data["total_amount"],
                due_dates=body.validated_data["due_dates"],
            )
        except LedgerError as error:
            raise ValidationError({"detail": str(error)})
        return Response(
            {"installments": InstallmentPreviewSerializer(rows, many=True).data}
        )


# ---- CSV import -----------------------------------------------------------


class ImportStudentsView(generics.GenericAPIView):
    """Upload a CSV of students. Without commit=true it only previews."""

    parser_classes = [MultiPartParser]
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = ImportUploadSerializer

    def post(self, request):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        upload = body.validated_data["file"]
        try:
            result = import_students(
                raw=upload.read(MAX_BYTES + 1),  # never read more than the limit
                commit=body.validated_data["commit"],
            )
        except ImportFileError as error:
            raise ValidationError({"detail": str(error)})

        if result["errors"]:
            return Response(result, status=400)
        return Response(result, status=201 if result["committed"] else 200)

# ---- Login and logout -----------------------------------------------------


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """Visiting this address makes Django set the csrftoken cookie."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({"detail": "CSRF cookie set."})


# DRF views skip Django's CSRF check by default. For login we switch it back on.
@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"  # the rate comes from DEFAULT_THROTTLE_RATES

    def post(self, request):
        body = LoginSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=body.validated_data["username"],
            password=body.validated_data["password"],
        )
        if user is None:  # also covers inactive accounts
            raise ValidationError({"detail": "Wrong username or password."})
        login(request, user)  # starts the session
        return Response({"id": user.id, "username": user.username, "role": user.role})


class LogoutView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        logout(request)  # safe to call even if nobody is logged in
        return Response(status=204)

# ---- Enrollments (owner screens) ------------------------------------------


class EnrollmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Lets the owner pick who needs a fee plan. Filters: ?without_plan=1,
    ?active=1 and ?search=name."""

    permission_classes = [permissions.IsAuthenticated, IsOwner]
    serializer_class = EnrollmentSerializer

    def get_queryset(self):
        queryset = (
            enrollments_for(self.request.user)
            .select_related("student", "batch")
            .annotate(
                has_fee_plan=Exists(
                    FeePlan.objects.filter(enrollment=OuterRef("pk"))
                )
            )
            .order_by("student__full_name", "id")
        )
        params = self.request.query_params
        if params.get("without_plan") == "1":
            queryset = queryset.filter(has_fee_plan=False)
        if params.get("active") == "1":
            queryset = queryset.filter(is_active=True)
        search = params.get("search", "").strip()
        if search:
            queryset = queryset.filter(student__full_name__icontains=search[:100])
        return queryset

# ---- Attendance (reading) -------------------------------------------------


class AttendanceViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read attendance. Owners see everything, teachers their own batches,
    parents their own children.
    Filters: ?student=ID  ?batch=ID  ?from=YYYY-MM-DD  ?to=YYYY-MM-DD"""

    serializer_class = AttendanceRecordSerializer

    def _scoped(self):
        params = self.request.query_params
        queryset = attendance_records_for(self.request.user)
        student = _digits(params.get("student"))
        if student is not None:
            queryset = queryset.filter(student_id=student)
        batch = _digits(params.get("batch"))
        if batch is not None:
            queryset = queryset.filter(session__batch_id=batch)
        start = _optional_date(self.request, "from")
        if start is not None:
            queryset = queryset.filter(session__date__gte=start)
        end = _optional_date(self.request, "to")
        if end is not None:
            queryset = queryset.filter(session__date__lte=end)
        return queryset

    def get_queryset(self):
        return (
            self._scoped()
            .select_related("student", "session__batch")
            .order_by("-session__date", "student__full_name", "session__batch__name", "id")
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Totals per student and batch, computed by the database in one query."""
        grouped = (
            self._scoped()
            .values(
                "student_id",
                "student__full_name",
                "session__batch_id",
                "session__batch__name",
            )
            .annotate(
                total=Count("id"),
                present=Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)),
                absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
                late=Count("id", filter=Q(status=AttendanceRecord.Status.LATE)),
                leave=Count("id", filter=Q(status=AttendanceRecord.Status.LEAVE)),
            )
            .order_by("student__full_name", "session__batch__name", "student_id")
        )
        data = []
        for row in grouped[:500]:  # a sensible upper limit
            counted = row["total"] - row["leave"]  # leave days do not count against
            attended = row["present"] + row["late"]
            data.append(
                {
                    "student": row["student_id"],
                    "student_name": row["student__full_name"],
                    "batch": row["session__batch_id"],
                    "batch_name": row["session__batch__name"],
                    "total": row["total"],
                    "present": row["present"],
                    "absent": row["absent"],
                    "late": row["late"],
                    "leave": row["leave"],
                    "percent": round(attended * 100 / counted, 1) if counted else None,
                }
            )
        return Response(data)

# ---- Accounts (owner screens) ---------------------------------------------


class UserViewSet(
    mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    """List accounts and create teacher or parent accounts. Owner only.
    Filters: ?role=teacher|student|owner  ?active=1  ?search=username"""

    permission_classes = [permissions.IsAuthenticated, IsOwner]

    def get_serializer_class(self):
        return UserCreateSerializer if self.action == "create" else UserSerializer

    def get_queryset(self):
        queryset = User.objects.order_by("username")
        params = self.request.query_params
        role = params.get("role")
        if role in User.Role.values:
            queryset = queryset.filter(role=role)
        if params.get("active") == "1":
            queryset = queryset.filter(is_active=True)
        search = params.get("search", "").strip()
        if search:
            queryset = queryset.filter(username__icontains=search[:100])
        return queryset

    def create(self, request, *args, **kwargs):
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = body.save()
        return Response(UserSerializer(user).data, status=201)