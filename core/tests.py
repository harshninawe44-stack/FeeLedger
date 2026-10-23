from datetime import date
from decimal import Decimal

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from .models import Batch, Enrollment, FeePlan, LedgerEntry, Student, User
from .services import LedgerError, net_paid, record_payment, reverse_payment


class LedgerTests(TestCase):
    def setUp(self):
        # Runs before every test, on a fresh empty test database.
        self.owner = User.objects.create_user(
            "owner1", password="pw", role=User.Role.OWNER
        )
        teacher = User.objects.create_user(
            "teacher1", password="pw", role=User.Role.TEACHER
        )
        batch = Batch.objects.create(
            name="JEE", teacher=teacher, start_date=date(2026, 6, 1)
        )
        student = Student.objects.create(full_name="Asha")
        enrollment = Enrollment.objects.create(
            student=student, batch=batch, joined_on=date(2026, 6, 1)
        )
        self.plan = FeePlan.objects.create(
            enrollment=enrollment, total_amount=Decimal("60000.00")
        )

    def pay(self, amount="15000.00"):
        return record_payment(
            fee_plan=self.plan,
            amount=Decimal(amount),
            method="cash",
            paid_on=date(2026, 7, 1),
            received_by=self.owner,
        )

    def test_payment_counts_towards_net_paid(self):
        self.pay("15000.00")
        self.assertEqual(net_paid(self.plan), Decimal("15000.00"))

    def test_decimal_arithmetic_is_exact(self):
        # With floats, 0.1 + 0.2 gives 0.30000000000000004.
        self.pay("0.10")
        self.pay("0.20")
        self.assertEqual(net_paid(self.plan), Decimal("0.30"))

    def test_reversal_cancels_payment_exactly(self):
        payment = self.pay("15000.00")
        reversal = reverse_payment(
            entry=payment, reason="Entered wrong student", received_by=self.owner
        )
        self.assertEqual(reversal.amount, Decimal("-15000.00"))
        self.assertEqual(reversal.reverses, payment)
        self.assertEqual(net_paid(self.plan), Decimal("0.00"))

    def test_payment_cannot_be_reversed_twice(self):
        payment = self.pay()
        reverse_payment(entry=payment, reason="Mistake", received_by=self.owner)
        with self.assertRaises(LedgerError):
            reverse_payment(entry=payment, reason="Again", received_by=self.owner)

    def test_a_reversal_cannot_be_reversed(self):
        payment = self.pay()
        reversal = reverse_payment(
            entry=payment, reason="Mistake", received_by=self.owner
        )
        with self.assertRaises(LedgerError):
            reverse_payment(entry=reversal, reason="Undo", received_by=self.owner)

    def test_reversal_needs_a_reason(self):
        payment = self.pay()
        with self.assertRaises(LedgerError):
            reverse_payment(entry=payment, reason="   ", received_by=self.owner)

    def test_zero_or_negative_payment_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.pay("0.00")
        with self.assertRaises(LedgerError):
            self.pay("-100.00")

    def test_overpayment_is_rejected(self):
        self.pay("60000.00")
        with self.assertRaises(LedgerError):
            self.pay("0.01")

    def test_reversal_frees_up_room_for_a_corrected_payment(self):
        wrong = self.pay("60000.00")
        reverse_payment(entry=wrong, reason="Wrong amount", received_by=self.owner)
        self.pay("45000.00")
        self.assertEqual(net_paid(self.plan), Decimal("45000.00"))

    def test_database_rejects_wrong_sign_even_when_bypassing_our_code(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LedgerEntry.objects.create(
                    fee_plan=self.plan,
                    kind=LedgerEntry.Kind.PAYMENT,
                    amount=Decimal("-5.00"),
                    method="cash",
                    paid_on=date(2026, 7, 1),
                    received_by=self.owner,
                )

    def test_database_rejects_updates_to_ledger_rows(self):
        entry = self.pay()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                LedgerEntry.objects.filter(pk=entry.pk).update(
                    amount=Decimal("1.00")
                )

    def test_database_rejects_deleting_ledger_rows(self):
        entry = self.pay()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                entry.delete()