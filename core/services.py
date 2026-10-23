from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import FeePlan, LedgerEntry


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