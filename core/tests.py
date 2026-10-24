from datetime import date
from decimal import Decimal

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from .models import Batch, Enrollment, FeePlan, LedgerEntry, Student, User
from .services import (
    LedgerError,
    create_installments,
    dues_report,
    net_paid,
    record_payment,
    reverse_payment,
    split_amount,
)

from django.contrib.auth.models import AnonymousUser

from .selectors import (
    batches_for,
    enrollments_for,
    fee_plans_for,
    ledger_entries_for,
    students_for,
)


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


class InstallmentTests(TestCase):
    def setUp(self):
        teacher = User.objects.create_user(
            "teacher2", password="pw", role=User.Role.TEACHER
        )
        batch = Batch.objects.create(
            name="NEET", teacher=teacher, start_date=date(2026, 6, 1)
        )
        student = Student.objects.create(full_name="Ravi")
        enrollment = Enrollment.objects.create(
            student=student, batch=batch, joined_on=date(2026, 6, 1)
        )
        self.plan = FeePlan.objects.create(
            enrollment=enrollment, total_amount=Decimal("10000.00")
        )

    def test_ten_thousand_split_in_three(self):
        self.assertEqual(
            split_amount("10000.00", 3),
            [Decimal("3333.34"), Decimal("3333.33"), Decimal("3333.33")],
        )

    def test_even_split(self):
        self.assertEqual(split_amount("60000.00", 4), [Decimal("15000.00")] * 4)

    def test_split_always_adds_up_to_the_total(self):
        for total in ["0.01", "1.00", "99.99", "10000.00", "59999.99", "123456.78"]:
            for parts in range(1, 13):
                amounts = split_amount(total, parts)
                self.assertEqual(len(amounts), parts)
                self.assertEqual(sum(amounts), Decimal(total))

    def test_no_installment_differs_by_more_than_one_paisa(self):
        amounts = split_amount("10000.00", 7)
        self.assertLessEqual(max(amounts) - min(amounts), Decimal("0.01"))

    def test_rejects_zero_parts(self):
        with self.assertRaises(LedgerError):
            split_amount("100.00", 0)

    def test_rejects_more_than_two_decimal_places(self):
        with self.assertRaises(LedgerError):
            split_amount("100.005", 2)

    def test_create_installments_saves_rows_that_sum_to_total(self):
        dues = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
        create_installments(fee_plan=self.plan, due_dates=dues)
        rows = list(self.plan.installments.order_by("number"))
        self.assertEqual([r.due_date for r in rows], dues)
        self.assertEqual(sum(r.amount for r in rows), Decimal("10000.00"))

    def test_installments_cannot_be_created_twice(self):
        dues = [date(2026, 7, 1), date(2026, 8, 1)]
        create_installments(fee_plan=self.plan, due_dates=dues)
        with self.assertRaises(LedgerError):
            create_installments(fee_plan=self.plan, due_dates=dues)

class DuesReportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "owner3", password="pw", role=User.Role.OWNER
        )
        teacher = User.objects.create_user(
            "teacher3", password="pw", role=User.Role.TEACHER
        )
        self.batch = Batch.objects.create(
            name="JEE", teacher=teacher, start_date=date(2026, 6, 1)
        )

    def make_plan(self, name):
        student = Student.objects.create(full_name=name)
        enrollment = Enrollment.objects.create(
            student=student, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        plan = FeePlan.objects.create(
            enrollment=enrollment, total_amount=Decimal("60000.00")
        )
        create_installments(
            fee_plan=plan,
            due_dates=[
                date(2026, 7, 1),
                date(2026, 8, 1),
                date(2026, 9, 1),
                date(2026, 10, 1),
            ],
        )  # 4 x 15000.00
        return plan

    def pay(self, plan, amount):
        return record_payment(
            fee_plan=plan,
            amount=Decimal(amount),
            method="cash",
            paid_on=date(2026, 7, 2),
            received_by=self.owner,
        )

    def row_for(self, plan, today):
        return dues_report(today=today).get(pk=plan.pk)

    def test_no_payments_means_everything_due_is_overdue(self):
        plan = self.make_plan("Asha")
        row = self.row_for(plan, date(2026, 8, 15))
        self.assertEqual(row.paid, Decimal("0.00"))
        self.assertEqual(row.due_so_far, Decimal("30000.00"))
        self.assertEqual(row.overdue, Decimal("30000.00"))
        self.assertEqual(row.balance, Decimal("60000.00"))

    def test_numbers_are_correct_with_many_installments_and_payments(self):
        # 4 installments x 3 ledger rows: a wrongly joined query would
        # multiply the totals. Subqueries must keep them exact.
        plan = self.make_plan("Asha")
        self.pay(plan, "15000.00")
        wrong = self.pay(plan, "10000.00")
        reverse_payment(entry=wrong, reason="Wrong student", received_by=self.owner)
        row = self.row_for(plan, date(2026, 8, 15))
        self.assertEqual(row.paid, Decimal("15000.00"))
        self.assertEqual(row.due_so_far, Decimal("30000.00"))
        self.assertEqual(row.overdue, Decimal("15000.00"))
        self.assertEqual(row.balance, Decimal("45000.00"))

    def test_paying_ahead_gives_zero_overdue_not_negative(self):
        plan = self.make_plan("Asha")
        self.pay(plan, "20000.00")
        row = self.row_for(plan, date(2026, 7, 15))  # only 15000 due so far
        self.assertEqual(row.overdue, Decimal("0.00"))
        self.assertEqual(row.balance, Decimal("40000.00"))

    def test_report_uses_one_query_no_matter_how_many_students(self):
        for i in range(5):
            plan = self.make_plan(f"Student {i}")
            self.pay(plan, "15000.00")
        with self.assertNumQueries(1):
            rows = list(dues_report(today=date(2026, 8, 15)))
            for r in rows:  # touching related data must not add queries
                r.enrollment.student.full_name
                r.enrollment.batch.name
                r.paid, r.overdue, r.balance
        self.assertEqual(len(rows), 5)

class ScopingTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher_a = mk("ta", password="pw", role=User.Role.TEACHER)
        self.teacher_b = mk("tb", password="pw", role=User.Role.TEACHER)
        self.parent_a = mk("pa", password="pw", role=User.Role.STUDENT)
        self.parent_b = mk("pb", password="pw", role=User.Role.STUDENT)

        self.batch_a = Batch.objects.create(
            name="A", teacher=self.teacher_a, start_date=date(2026, 6, 1)
        )
        self.batch_b = Batch.objects.create(
            name="B", teacher=self.teacher_b, start_date=date(2026, 6, 1)
        )
        self.asha = Student.objects.create(full_name="Asha", user=self.parent_a)
        self.bala = Student.objects.create(full_name="Bala", user=self.parent_b)
        self.enr_a = Enrollment.objects.create(
            student=self.asha, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        self.enr_b = Enrollment.objects.create(
            student=self.bala, batch=self.batch_b, joined_on=date(2026, 6, 1)
        )
        self.plan_a = FeePlan.objects.create(
            enrollment=self.enr_a, total_amount=Decimal("10000.00")
        )
        self.plan_b = FeePlan.objects.create(
            enrollment=self.enr_b, total_amount=Decimal("10000.00")
        )
        self.pay_a = record_payment(
            fee_plan=self.plan_a, amount=Decimal("1000.00"), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.owner,
        )
        record_payment(
            fee_plan=self.plan_b, amount=Decimal("2000.00"), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.owner,
        )

    def test_owner_sees_everything(self):
        self.assertEqual(batches_for(self.owner).count(), 2)
        self.assertEqual(students_for(self.owner).count(), 2)
        self.assertEqual(fee_plans_for(self.owner).count(), 2)
        self.assertEqual(ledger_entries_for(self.owner).count(), 2)

    def test_teacher_sees_only_own_batch_and_its_students(self):
        self.assertEqual(list(batches_for(self.teacher_a)), [self.batch_a])
        self.assertEqual(list(students_for(self.teacher_a)), [self.asha])
        self.assertEqual(list(enrollments_for(self.teacher_a)), [self.enr_a])

    def test_teacher_cannot_reach_another_teachers_batch(self):
        self.assertFalse(
            batches_for(self.teacher_a).filter(pk=self.batch_b.pk).exists()
        )
        self.assertFalse(
            students_for(self.teacher_a).filter(pk=self.bala.pk).exists()
        )

    def test_teacher_sees_no_fee_data(self):
        self.assertEqual(fee_plans_for(self.teacher_a).count(), 0)
        self.assertEqual(ledger_entries_for(self.teacher_a).count(), 0)

    def test_parent_sees_only_own_child(self):
        self.assertEqual(list(students_for(self.parent_a)), [self.asha])
        self.assertEqual(list(batches_for(self.parent_a)), [self.batch_a])
        self.assertEqual(list(fee_plans_for(self.parent_a)), [self.plan_a])
        self.assertEqual(list(ledger_entries_for(self.parent_a)), [self.pay_a])

    def test_parent_cannot_reach_another_familys_data(self):
        self.assertFalse(
            fee_plans_for(self.parent_a).filter(pk=self.plan_b.pk).exists()
        )
        self.assertFalse(
            students_for(self.parent_a).filter(pk=self.bala.pk).exists()
        )

    def test_parent_with_two_children_in_one_batch_sees_it_once(self):
        brother = Student.objects.create(full_name="Chetan", user=self.parent_a)
        Enrollment.objects.create(
            student=brother, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        self.assertEqual(batches_for(self.parent_a).count(), 1)
        self.assertEqual(students_for(self.parent_a).count(), 2)

    def test_anonymous_user_sees_nothing(self):
        nobody = AnonymousUser()
        self.assertEqual(batches_for(nobody).count(), 0)
        self.assertEqual(students_for(nobody).count(), 0)
        self.assertEqual(fee_plans_for(nobody).count(), 0)

    def test_dues_report_respects_scoping(self):
        today = date(2026, 8, 1)
        rows = dues_report(fee_plans=fee_plans_for(self.parent_a), today=today)
        self.assertEqual([r.pk for r in rows], [self.plan_a.pk])
        rows = dues_report(fee_plans=fee_plans_for(self.teacher_a), today=today)
        self.assertEqual(len(list(rows)), 0)

    def test_superuser_is_automatically_the_owner(self):
        root = User.objects.create_superuser("root", password="pw")
        self.assertEqual(root.role, User.Role.OWNER)