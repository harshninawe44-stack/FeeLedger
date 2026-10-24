from datetime import date, timedelta
from django.utils import timezone
from decimal import Decimal

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from .models import (
    AttendanceChange,
    AttendanceRecord,
    AttendanceSession,
    Batch,
    Enrollment,
    FeePlan,
    LedgerEntry,
    Student,
    User,
)

from .services import (
    AttendanceError,
    LedgerError,
    create_installments,
    dues_report,
    mark_attendance,
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

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient


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

S = AttendanceRecord.Status


class AttendanceTests(TestCase):
    TODAY = date(2026, 10, 24)
    DAY = date(2026, 10, 20)  # 4 days before TODAY

    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher_a = mk("ta", password="pw", role=User.Role.TEACHER)
        self.teacher_b = mk("tb", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)

        self.batch_a = Batch.objects.create(
            name="A", teacher=self.teacher_a, start_date=date(2026, 6, 1)
        )
        self.asha = Student.objects.create(full_name="Asha", user=self.parent)
        self.bala = Student.objects.create(full_name="Bala")
        self.chetan = Student.objects.create(full_name="Chetan")  # has left
        for student, active in [(self.asha, True), (self.bala, True), (self.chetan, False)]:
            Enrollment.objects.create(
                student=student,
                batch=self.batch_a,
                joined_on=date(2026, 6, 1),
                is_active=active,
            )

    def sheet(self):
        return {self.asha.id: S.PRESENT, self.bala.id: S.ABSENT}

    def mark(self, user=None, statuses=None, day=None):
        return mark_attendance(
            batch=self.batch_a,
            date=day or self.DAY,
            statuses=statuses or self.sheet(),
            marked_by=user or self.teacher_a,
            today=self.TODAY,
        )

    def test_marks_whole_batch_in_one_call(self):
        session = self.mark()
        self.assertEqual(session.records.count(), 2)
        self.assertEqual(session.records.get(student=self.asha).status, S.PRESENT)
        self.assertEqual(session.records.get(student=self.bala).status, S.ABSENT)
        # first marking is logged with an empty "old" status
        self.assertEqual(AttendanceChange.objects.count(), 2)
        self.assertEqual(AttendanceChange.objects.filter(old_status="").count(), 2)

    def test_editing_a_status_writes_an_audit_entry(self):
        self.mark()
        edited = self.sheet()
        edited[self.bala.id] = S.LATE
        self.mark(statuses=edited)
        record = AttendanceRecord.objects.get(student=self.bala)
        self.assertEqual(record.status, S.LATE)
        change = AttendanceChange.objects.order_by("id").last()
        self.assertEqual(change.old_status, S.ABSENT)
        self.assertEqual(change.new_status, S.LATE)
        self.assertEqual(change.changed_by, self.teacher_a)
        self.assertEqual(AttendanceChange.objects.count(), 3)

    def test_resubmitting_the_same_sheet_creates_no_new_changes(self):
        self.mark()
        self.mark()
        self.assertEqual(AttendanceChange.objects.count(), 2)

    def test_teacher_cannot_mark_another_teachers_batch(self):
        with self.assertRaises(AttendanceError):
            self.mark(user=self.teacher_b)

    def test_parent_cannot_mark_attendance(self):
        with self.assertRaises(AttendanceError):
            self.mark(user=self.parent)

    def test_owner_can_mark_any_batch(self):
        self.mark(user=self.owner)
        self.assertEqual(AttendanceSession.objects.count(), 1)

    def test_future_date_is_rejected(self):
        with self.assertRaises(AttendanceError):
            self.mark(day=date(2026, 10, 25))

    def test_teacher_is_limited_to_the_edit_window_but_owner_is_not(self):
        self.mark(day=date(2026, 10, 17))  # exactly 7 days ago: allowed
        old_day = date(2026, 10, 10)  # 14 days ago
        with self.assertRaises(AttendanceError):
            self.mark(day=old_day)
        self.mark(user=self.owner, day=old_day)

    def test_incomplete_sheet_is_rejected(self):
        with self.assertRaises(AttendanceError):
            self.mark(statuses={self.asha.id: S.PRESENT})
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_student_who_left_the_batch_cannot_be_marked(self):
        sheet = self.sheet()
        sheet[self.chetan.id] = S.PRESENT
        with self.assertRaises(AttendanceError):
            self.mark(statuses=sheet)

    def test_invalid_status_is_rejected(self):
        sheet = self.sheet()
        sheet[self.bala.id] = "sleeping"
        with self.assertRaises(AttendanceError):
            self.mark(statuses=sheet)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_only_one_session_per_batch_per_date(self):
        self.mark()
        self.mark()
        self.assertEqual(AttendanceSession.objects.count(), 1)
        self.assertEqual(AttendanceRecord.objects.count(), 2)

class ApiTests(TestCase):
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
        enr_a = Enrollment.objects.create(
            student=self.asha, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        enr_b = Enrollment.objects.create(
            student=self.bala, batch=self.batch_b, joined_on=date(2026, 6, 1)
        )
        self.plan_a = FeePlan.objects.create(
            enrollment=enr_a, total_amount=Decimal("10000.00")
        )
        self.plan_b = FeePlan.objects.create(
            enrollment=enr_b, total_amount=Decimal("10000.00")
        )
        record_payment(
            fee_plan=self.plan_a, amount=Decimal("1000.00"), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.owner,
        )
        record_payment(
            fee_plan=self.plan_b, amount=Decimal("2000.00"), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.owner,
        )
        self.api = APIClient()

    def get(self, user, url):
        self.api.force_authenticate(user=user)
        return self.api.get(url)

    def count_queries(self, user, url):
        self.api.force_authenticate(user=user)
        with CaptureQueriesContext(connection) as ctx:
            self.api.get(url)
        return len(ctx)

    def test_anonymous_user_is_rejected(self):
        response = self.get(None, reverse("batch-list"))
        self.assertEqual(response.status_code, 403)

    def test_me_returns_the_role(self):
        response = self.get(self.teacher_a, reverse("me"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "teacher")

    def test_teacher_lists_only_own_batches(self):
        response = self.get(self.teacher_a, reverse("batch-list"))
        names = [b["name"] for b in response.data["results"]]
        self.assertEqual(names, ["A"])

    def test_teacher_gets_404_for_another_teachers_batch(self):
        url = reverse("batch-detail", args=[self.batch_b.pk])
        self.assertEqual(self.get(self.teacher_a, url).status_code, 404)
        self.assertEqual(self.get(self.teacher_b, url).status_code, 200)

    def test_parent_gets_404_for_another_familys_student(self):
        url = reverse("student-detail", args=[self.bala.pk])
        self.assertEqual(self.get(self.parent_a, url).status_code, 404)

    def test_teacher_is_forbidden_from_dues(self):
        response = self.get(self.teacher_a, reverse("dues"))
        self.assertEqual(response.status_code, 403)

    def test_parent_sees_only_own_dues_and_money_is_a_string(self):
        response = self.get(self.parent_a, reverse("dues"))
        rows = response.data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student"], "Asha")
        self.assertEqual(rows[0]["total_amount"], "10000.00")
        self.assertEqual(rows[0]["paid"], "1000.00")
        self.assertEqual(rows[0]["balance"], "9000.00")

    def test_owner_sees_all_dues(self):
        response = self.get(self.owner, reverse("dues"))
        self.assertEqual(len(response.data["results"]), 2)

    def test_dues_query_count_does_not_grow_with_more_students(self):
        before = self.count_queries(self.owner, reverse("dues"))
        for i in range(5):
            student = Student.objects.create(full_name=f"Extra {i}")
            enr = Enrollment.objects.create(
                student=student, batch=self.batch_a, joined_on=date(2026, 6, 1)
            )
            plan = FeePlan.objects.create(
                enrollment=enr, total_amount=Decimal("5000.00")
            )
            record_payment(
                fee_plan=plan, amount=Decimal("100.00"), method="cash",
                paid_on=date(2026, 7, 1), received_by=self.owner,
            )
        after = self.count_queries(self.owner, reverse("dues"))
        self.assertEqual(before, after)

    def test_batch_list_query_count_does_not_grow_with_more_batches(self):
        before = self.count_queries(self.owner, reverse("batch-list"))
        for i in range(5):
            Batch.objects.create(
                name=f"Extra {i}", teacher=self.teacher_a, start_date=date(2026, 6, 1)
            )
        after = self.count_queries(self.owner, reverse("batch-list"))
        self.assertEqual(before, after)

    def test_api_is_read_only(self):
        self.api.force_authenticate(user=self.owner)
        response = self.api.post(reverse("batch-list"), {"name": "Hacked"})
        self.assertEqual(response.status_code, 405)

class ApiWriteTests(TestCase):
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
        self.ravi = Student.objects.create(full_name="Ravi")
        self.bala = Student.objects.create(full_name="Bala", user=self.parent_b)
        enr_asha = Enrollment.objects.create(
            student=self.asha, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        Enrollment.objects.create(
            student=self.ravi, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        enr_bala = Enrollment.objects.create(
            student=self.bala, batch=self.batch_b, joined_on=date(2026, 6, 1)
        )
        self.plan_a = FeePlan.objects.create(
            enrollment=enr_asha, total_amount=Decimal("10000.00")
        )
        self.plan_b = FeePlan.objects.create(
            enrollment=enr_bala, total_amount=Decimal("10000.00")
        )
        self.today = timezone.localdate()
        self.api = APIClient()

    # ---- helpers ----
    def get(self, user, url):
        self.api.force_authenticate(user=user)
        return self.api.get(url)

    def post(self, user, url, body):
        self.api.force_authenticate(user=user)
        return self.api.post(url, body, format="json")

    def make_payment(self, plan=None, amount="1000.00"):
        return record_payment(
            fee_plan=plan or self.plan_a,
            amount=Decimal(amount),
            method="cash",
            paid_on=date(2026, 10, 1),
            received_by=self.owner,
        )

    def payment_body(self, amount, plan=None):
        return {
            "fee_plan": (plan or self.plan_a).pk,
            "amount": amount,
            "method": "cash",
            "paid_on": "2026-10-01",
        }

    def sheet_body(self, day=None, ravi="absent"):
        return {
            "date": str(day or self.today),
            "records": [
                {"student": self.asha.pk, "status": "present"},
                {"student": self.ravi.pk, "status": ravi},
            ],
        }

    def attendance_url(self, batch=None):
        return reverse("batch-attendance", args=[(batch or self.batch_a).pk])

    # ---- payments ----
    def test_owner_records_a_payment(self):
        response = self.post(
            self.owner, reverse("payment-list"), self.payment_body("1000.00")
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["amount"], "1000.00")
        self.assertEqual(response.data["kind"], "payment")
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.get().received_by, self.owner)

    def test_teacher_cannot_record_a_payment(self):
        response = self.post(
            self.teacher_a, reverse("payment-list"), self.payment_body("1000.00")
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_parent_cannot_record_a_payment(self):
        response = self.post(
            self.parent_a, reverse("payment-list"), self.payment_body("1000.00")
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_overpayment_returns_400(self):
        response = self.post(
            self.owner, reverse("payment-list"), self.payment_body("10000.01")
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_bad_amounts_return_400(self):
        for amount in ["0.00", "-5.00", "100.005", "abc"]:
            response = self.post(
                self.owner, reverse("payment-list"), self.payment_body(amount)
            )
            self.assertEqual(response.status_code, 400, amount)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_unknown_fee_plan_returns_404(self):
        body = self.payment_body("100.00")
        body["fee_plan"] = 99999
        response = self.post(self.owner, reverse("payment-list"), body)
        self.assertEqual(response.status_code, 404)

    def test_owner_reverses_a_payment(self):
        entry = self.make_payment(amount="1000.00")
        response = self.post(
            self.owner,
            reverse("payment-reverse", args=[entry.pk]),
            {"reason": "Entered wrong student"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["kind"], "reversal")
        self.assertEqual(response.data["amount"], "-1000.00")
        self.assertEqual(response.data["reverses"], entry.pk)
        self.assertEqual(net_paid(self.plan_a), Decimal("0.00"))

    def test_payment_cannot_be_reversed_twice(self):
        entry = self.make_payment()
        url = reverse("payment-reverse", args=[entry.pk])
        self.assertEqual(self.post(self.owner, url, {"reason": "x"}).status_code, 201)
        self.assertEqual(self.post(self.owner, url, {"reason": "y"}).status_code, 400)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_reversal_needs_a_reason(self):
        entry = self.make_payment()
        url = reverse("payment-reverse", args=[entry.pk])
        for reason in ["", "   "]:
            response = self.post(self.owner, url, {"reason": reason})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def test_teacher_and_parent_cannot_reverse(self):
        entry = self.make_payment()
        url = reverse("payment-reverse", args=[entry.pk])
        self.assertEqual(self.post(self.teacher_a, url, {"reason": "x"}).status_code, 403)
        self.assertEqual(self.post(self.parent_a, url, {"reason": "x"}).status_code, 403)
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def test_parent_sees_only_own_payments(self):
        mine = self.make_payment(self.plan_a)
        self.make_payment(self.plan_b)
        response = self.get(self.parent_a, reverse("payment-list"))
        self.assertEqual([r["id"] for r in response.data["results"]], [mine.pk])

    def test_teacher_is_forbidden_from_payments(self):
        self.make_payment()
        response = self.get(self.teacher_a, reverse("payment-list"))
        self.assertEqual(response.status_code, 403)

    def test_payments_cannot_be_edited_or_deleted_through_the_api(self):
        entry = self.make_payment()
        url = reverse("payment-detail", args=[entry.pk])
        self.api.force_authenticate(user=self.owner)
        self.assertEqual(self.api.patch(url, {"amount": "1.00"}, format="json").status_code, 405)
        self.assertEqual(self.api.put(url, {"amount": "1.00"}, format="json").status_code, 405)
        self.assertEqual(self.api.delete(url).status_code, 405)
        entry.refresh_from_db()
        self.assertEqual(entry.amount, Decimal("1000.00"))

    def test_payment_list_query_count_does_not_grow(self):
        self.make_payment()
        url = reverse("payment-list")
        self.api.force_authenticate(user=self.owner)
        with CaptureQueriesContext(connection) as before:
            self.api.get(url)
        for _ in range(5):
            self.make_payment(amount="10.00")
        with CaptureQueriesContext(connection) as after:
            self.api.get(url)
        self.assertEqual(len(before), len(after))

    # ---- attendance ----
    def test_teacher_reads_own_batch_sheet(self):
        response = self.get(self.teacher_a, self.attendance_url())
        self.assertEqual(response.status_code, 200)
        names = [r["student_name"] for r in response.data["records"]]
        self.assertEqual(names, ["Asha", "Ravi"])
        self.assertTrue(all(r["status"] is None for r in response.data["records"]))

    def test_teacher_submits_attendance(self):
        response = self.post(self.teacher_a, self.attendance_url(), self.sheet_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AttendanceSession.objects.count(), 1)
        self.assertEqual(AttendanceRecord.objects.get(student=self.asha).status, "present")
        self.assertEqual(AttendanceRecord.objects.get(student=self.ravi).status, "absent")
        statuses = [r["status"] for r in response.data["records"]]
        self.assertEqual(statuses, ["present", "absent"])

    def test_teacher_gets_404_for_another_teachers_batch_attendance(self):
        self.assertEqual(self.get(self.teacher_b, self.attendance_url()).status_code, 404)
        response = self.post(self.teacher_b, self.attendance_url(), self.sheet_body())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_parent_cannot_use_attendance_endpoint(self):
        self.assertEqual(self.get(self.parent_a, self.attendance_url()).status_code, 403)
        response = self.post(self.parent_a, self.attendance_url(), self.sheet_body())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_incomplete_sheet_returns_400(self):
        body = {
            "date": str(self.today),
            "records": [{"student": self.asha.pk, "status": "present"}],
        }
        response = self.post(self.teacher_a, self.attendance_url(), body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_duplicate_student_returns_400(self):
        body = {
            "date": str(self.today),
            "records": [
                {"student": self.asha.pk, "status": "present"},
                {"student": self.asha.pk, "status": "absent"},
            ],
        }
        response = self.post(self.teacher_a, self.attendance_url(), body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_unknown_status_returns_400(self):
        response = self.post(
            self.teacher_a, self.attendance_url(), self.sheet_body(ravi="sleeping")
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_future_date_returns_400(self):
        tomorrow = self.today + timedelta(days=1)
        response = self.post(
            self.teacher_a, self.attendance_url(), self.sheet_body(day=tomorrow)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttendanceSession.objects.count(), 0)

    def test_editing_attendance_logs_the_change(self):
        self.post(self.teacher_a, self.attendance_url(), self.sheet_body())
        self.post(self.teacher_a, self.attendance_url(), self.sheet_body(ravi="late"))
        self.assertEqual(AttendanceChange.objects.count(), 3)  # 2 first marks + 1 edit
        change = AttendanceChange.objects.order_by("id").last()
        self.assertEqual((change.old_status, change.new_status), ("absent", "late"))
        self.assertEqual(change.changed_by, self.teacher_a)