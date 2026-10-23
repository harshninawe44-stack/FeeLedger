from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import FeePlan, Installment, LedgerEntry


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