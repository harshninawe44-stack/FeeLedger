from .models import Batch, Enrollment, FeePlan, LedgerEntry, Student, User


def _role(user):
    """The user's role, or None if nobody is logged in."""
    if user is None or not user.is_authenticated:
        return None
    return user.role


def batches_for(user):
    role = _role(user)
    if role == User.Role.OWNER:
        return Batch.objects.all()
    if role == User.Role.TEACHER:
        return Batch.objects.filter(teacher=user)
    if role == User.Role.STUDENT:
        # distinct(): a parent with two children in one batch must see it once
        return Batch.objects.filter(enrollments__student__user=user).distinct()
    return Batch.objects.none()


def students_for(user):
    role = _role(user)
    if role == User.Role.OWNER:
        return Student.objects.all()
    if role == User.Role.TEACHER:
        return Student.objects.filter(enrollments__batch__teacher=user).distinct()
    if role == User.Role.STUDENT:
        return Student.objects.filter(user=user)
    return Student.objects.none()


def enrollments_for(user):
    role = _role(user)
    if role == User.Role.OWNER:
        return Enrollment.objects.all()
    if role == User.Role.TEACHER:
        return Enrollment.objects.filter(batch__teacher=user)
    if role == User.Role.STUDENT:
        return Enrollment.objects.filter(student__user=user)
    return Enrollment.objects.none()


def fee_plans_for(user):
    role = _role(user)
    if role == User.Role.OWNER:
        return FeePlan.objects.all()
    if role == User.Role.STUDENT:
        return FeePlan.objects.filter(enrollment__student__user=user)
    return FeePlan.objects.none()  # teachers and anonymous users: no fee data


def ledger_entries_for(user):
    role = _role(user)
    if role == User.Role.OWNER:
        return LedgerEntry.objects.all()
    if role == User.Role.STUDENT:
        return LedgerEntry.objects.filter(fee_plan__enrollment__student__user=user)
    return LedgerEntry.objects.none()