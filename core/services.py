from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from .models import (
    AttendanceChange,
    AttendanceRecord,
    AttendanceSession,
    Enrollment,
    FeePlan,
    Installment,
    LedgerEntry,
    User,
)
from .selectors import batches_for


class LedgerError(Exception):
    """Raised when an action would break a ledger rule."""


def net_paid(fee_plan):
    """Total received so far: payments minus reversals, summed by the database."""
    result = fee_plan.ledger_entries.aggregate(
        total=Coalesce(
            Sum("amount"),
            Value(Decimal("0.00")),
            output_field=DecimalField(),
        )
    )
    return result["total"]


@transaction.atomic
def record_payment(*, fee_plan, amount, method, paid_on, received_by,
                   reference="", note=""):
    amount = Decimal(amount)
    if amount <= 0:
        raise LedgerError("Payment amount must be positive.")

    # Lock this fee plan so two simultaneous payments can't both slip
    # past the overpayment check below.
    plan = FeePlan.objects.select_for_update().get(pk=fee_plan.pk)

    if net_paid(plan) + amount > plan.total_amount:
        raise LedgerError("Payment would exceed the total fee.")

    return LedgerEntry.objects.create(
        fee_plan=plan,
        kind=LedgerEntry.Kind.PAYMENT,
        amount=amount,
        method=method,
        reference=reference,
        note=note,
        paid_on=paid_on,
        received_by=received_by,
    )


@transaction.atomic
def reverse_payment(*, entry, reason, received_by, paid_on=None):
    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("A reason is required to reverse a payment.")

    # Lock the row so the same payment can't be reversed twice at once.
    entry = LedgerEntry.objects.select_for_update().get(pk=entry.pk)

    if entry.kind != LedgerEntry.Kind.PAYMENT:
        raise LedgerError("Only payments can be reversed.")
    if LedgerEntry.objects.filter(reverses=entry).exists():
        raise LedgerError("This payment has already been reversed.")

    return LedgerEntry.objects.create(
        fee_plan=entry.fee_plan,
        kind=LedgerEntry.Kind.REVERSAL,
        amount=-entry.amount,  # exact opposite, so the pair always cancels
        method=entry.method,
        reference=entry.reference,
        note=reason,
        paid_on=paid_on or timezone.localdate(),
        reverses=entry,
        received_by=received_by,
    )

def split_amount(total, parts):
    """Split a total into `parts` amounts that add up to exactly the total."""
    total = Decimal(total)
    if parts < 1:
        raise LedgerError("Need at least one installment.")
    if total <= 0:
        raise LedgerError("Total must be positive.")
    if total != total.quantize(Decimal("0.01")):
        raise LedgerError("Total can have at most 2 decimal places.")

    paise = int(total.scaleb(2))  # 10000.00 rupees -> 1000000 paise
    base, leftover = divmod(paise, parts)
    # The first `leftover` installments each get one extra paisa.
    amounts = [base + 1 if i < leftover else base for i in range(parts)]
    return [Decimal(a).scaleb(-2) for a in amounts]  # back to rupees


@transaction.atomic
def create_installments(*, fee_plan, due_dates):
    plan = FeePlan.objects.select_for_update().get(pk=fee_plan.pk)
    if plan.installments.exists():
        raise LedgerError("This fee plan already has installments.")

    amounts = split_amount(plan.total_amount, len(due_dates))
    return Installment.objects.bulk_create(
        [
            Installment(fee_plan=plan, number=i + 1, amount=amount, due_date=due)
            for i, (amount, due) in enumerate(zip(amounts, due_dates))
        ]
    )

MONEY = DecimalField(max_digits=12, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY)


def _total_per_plan(queryset, field):
    """A small query: the sum of `field` for one fee plan (the outer row)."""
    return Coalesce(
        Subquery(
            queryset.filter(fee_plan=OuterRef("pk"))
            .order_by()  # clear default ordering so grouping stays clean
            .values("fee_plan")
            .annotate(total=Sum(field))
            .values("total"),
            output_field=MONEY,
        ),
        ZERO,
    )


def dues_report(*, fee_plans=None, today=None):
    """One row per fee plan, with paid / due so far / overdue / balance.

    Everything is computed by the database in a single query.
    """
    today = today or timezone.localdate()
    if fee_plans is None:
        fee_plans = FeePlan.objects.all()

    return (
        fee_plans.select_related("enrollment__student", "enrollment__batch")
        .annotate(
            paid=_total_per_plan(LedgerEntry.objects.all(), "amount"),
            due_so_far=_total_per_plan(
                Installment.objects.filter(due_date__lte=today), "amount"
            ),
        )
        .annotate(
            balance=F("total_amount") - F("paid"),
            overdue=Greatest(F("due_so_far") - F("paid"), ZERO, output_field=MONEY),
        )
        .order_by("enrollment__batch__name", "enrollment__student__full_name")
    )

TEACHER_EDIT_WINDOW_DAYS = 7


class AttendanceError(Exception):
    """Raised when an attendance action would break a rule."""


@transaction.atomic
def mark_attendance(*, batch, date, statuses, marked_by, today=None):
    """Save a whole batch's attendance for one date.

    `statuses` maps student id -> status, e.g. {5: "present", 6: "absent"}.
    Calling it again for the same batch and date edits the sheet, and every
    change is written to the audit log.
    """
    today = today or timezone.localdate()

    if marked_by.role not in (User.Role.OWNER, User.Role.TEACHER):
        raise AttendanceError("Only owners and teachers can mark attendance.")
    if not batches_for(marked_by).filter(pk=batch.pk).exists():
        raise AttendanceError("Batch not found.")
    if date > today:
        raise AttendanceError("Cannot mark attendance for a future date.")
    if (
        marked_by.role == User.Role.TEACHER
        and (today - date).days > TEACHER_EDIT_WINDOW_DAYS
    ):
        raise AttendanceError(
            f"Teachers can only mark the last {TEACHER_EDIT_WINDOW_DAYS} days. "
            "Ask the owner to make this change."
        )

    enrolled_ids = set(
        Enrollment.objects.filter(batch=batch, is_active=True).values_list(
            "student_id", flat=True
        )
    )
    if set(statuses) != enrolled_ids:
        raise AttendanceError(
            "The sheet must list exactly the students enrolled in this batch."
        )
    valid = set(AttendanceRecord.Status.values)
    if any(status not in valid for status in statuses.values()):
        raise AttendanceError("Unknown attendance status.")

    session, _ = AttendanceSession.objects.get_or_create(
        batch=batch, date=date, defaults={"created_by": marked_by}
    )
    existing = {r.student_id: r for r in session.records.all()}

    new_records, edited_records, changes = [], [], []
    for student_id, status in statuses.items():
        record = existing.get(student_id)
        if record is None:
            new_records.append(
                AttendanceRecord(session=session, student_id=student_id, status=status)
            )
        elif record.status != status:
            changes.append(
                AttendanceChange(
                    record=record,
                    old_status=record.status,
                    new_status=status,
                    changed_by=marked_by,
                )
            )
            record.status = status
            edited_records.append(record)

    created = AttendanceRecord.objects.bulk_create(new_records)
    AttendanceRecord.objects.bulk_update(edited_records, ["status"])
    changes += [
        AttendanceChange(
            record=r, old_status="", new_status=r.status, changed_by=marked_by
        )
        for r in created
    ]
    AttendanceChange.objects.bulk_create(changes)
    return session