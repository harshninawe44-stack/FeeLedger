from datetime import date, timedelta
from django.utils import timezone
from decimal import Decimal

from django.core.cache import cache

from django.core.files.uploadedfile import SimpleUploadedFile

from .importers import ImportFileError, import_students

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
    Installment
)

from .services import (
    AttendanceError,
    LedgerError,
    create_fee_plan,
    create_installments,
    dues_report,
    mark_attendance,
    net_paid,
    preview_installments,
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

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from .management.commands.seed_demo import (
    DEMO_PASSWORD,
    OWNER_USERNAME,
    PARENT_USERNAMES,
    TEACHER_USERNAMES,
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
        for total in ["0.12", "1.00", "99.99", "10000.00", "59999.99", "123456.78"]:
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

    def test_students_and_batches_cannot_be_deleted_or_replaced(self):
        self.api.force_authenticate(user=self.owner)
        for name, pk in (
            ("student-detail", self.asha.pk),
            ("batch-detail", self.batch_a.pk),
        ):
            url = reverse(name, args=[pk])
            self.assertEqual(self.api.delete(url).status_code, 405, name)
            response = self.api.put(url, {"name": "X"}, format="json")
            self.assertEqual(response.status_code, 405, name)

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

class FeePlanServiceTests(TestCase):
    def setUp(self):
        teacher = User.objects.create_user(
            "t_fp", password="pw", role=User.Role.TEACHER
        )
        batch = Batch.objects.create(
            name="FP", teacher=teacher, start_date=date(2026, 6, 1)
        )
        self.student = Student.objects.create(full_name="Meera")
        self.enrollment = Enrollment.objects.create(
            student=self.student, batch=batch, joined_on=date(2026, 6, 1)
        )
        self.dues = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]

    def test_creates_plan_with_installments_summing_to_total(self):
        plan = create_fee_plan(
            enrollment=self.enrollment, total_amount="10000.00", due_dates=self.dues
        )
        rows = list(plan.installments.order_by("number"))
        self.assertEqual(plan.total_amount, Decimal("10000.00"))
        self.assertEqual([r.number for r in rows], [1, 2, 3])
        self.assertEqual([r.due_date for r in rows], self.dues)
        self.assertEqual(sum(r.amount for r in rows), Decimal("10000.00"))

    def test_earliest_installment_gets_the_extra_paisa(self):
        plan = create_fee_plan(
            enrollment=self.enrollment,
            total_amount="10000.00",
            due_dates=list(reversed(self.dues)),  # given out of order
        )
        first = plan.installments.get(number=1)
        self.assertEqual(first.due_date, date(2026, 7, 1))
        self.assertEqual(first.amount, Decimal("3333.34"))

    def test_requires_at_least_one_due_date(self):
        with self.assertRaises(LedgerError):
            create_fee_plan(
                enrollment=self.enrollment, total_amount="100.00", due_dates=[]
            )
        self.assertEqual(FeePlan.objects.count(), 0)

    def test_second_plan_for_same_enrollment_is_rejected(self):
        create_fee_plan(
            enrollment=self.enrollment, total_amount="100.00", due_dates=self.dues
        )
        with self.assertRaises(LedgerError):
            create_fee_plan(
                enrollment=self.enrollment, total_amount="200.00", due_dates=self.dues
            )
        self.assertEqual(FeePlan.objects.count(), 1)

    def test_inactive_enrollment_is_rejected(self):
        self.enrollment.is_active = False
        self.enrollment.save()
        with self.assertRaises(LedgerError):
            create_fee_plan(
                enrollment=self.enrollment, total_amount="100.00", due_dates=self.dues
            )
        self.assertEqual(FeePlan.objects.count(), 0)

    def test_zero_total_saves_nothing(self):
        with self.assertRaises(LedgerError):
            create_fee_plan(
                enrollment=self.enrollment, total_amount="0.00", due_dates=self.dues
            )
        self.assertEqual(FeePlan.objects.count(), 0)
        self.assertEqual(Installment.objects.count(), 0)


class FeePlanApiTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher = mk("tea", password="pw", role=User.Role.TEACHER)
        self.parent_a = mk("pa", password="pw", role=User.Role.STUDENT)
        self.parent_b = mk("pb", password="pw", role=User.Role.STUDENT)
        self.batch = Batch.objects.create(
            name="A", teacher=self.teacher, start_date=date(2026, 6, 1)
        )
        self.asha = Student.objects.create(full_name="Asha", user=self.parent_a)
        self.bala = Student.objects.create(full_name="Bala", user=self.parent_b)
        self.enr_a = Enrollment.objects.create(
            student=self.asha, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        self.enr_b = Enrollment.objects.create(
            student=self.bala, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        self.api = APIClient()

    def get(self, user, url):
        self.api.force_authenticate(user=user)
        return self.api.get(url)

    def post(self, user, body):
        self.api.force_authenticate(user=user)
        return self.api.post(reverse("feeplan-list"), body, format="json")

    def body(self, enrollment=None, total="10000.00", dues=None):
        return {
            "enrollment": (enrollment or self.enr_a).pk,
            "total_amount": total,
            "due_dates": dues
            if dues is not None
            else ["2026-11-01", "2026-12-01", "2027-01-01"],
        }

    def test_owner_creates_plan_with_exact_installments(self):
        response = self.post(self.owner, self.body())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["total_amount"], "10000.00")
        amounts = [i["amount"] for i in response.data["installments"]]
        self.assertEqual(amounts, ["3333.34", "3333.33", "3333.33"])
        self.assertEqual(FeePlan.objects.count(), 1)
        self.assertEqual(Installment.objects.count(), 3)

    def test_teacher_and_parent_cannot_create_plans(self):
        for user in (self.teacher, self.parent_a):
            response = self.post(user, self.body())
            self.assertEqual(response.status_code, 403)
        self.assertEqual(FeePlan.objects.count(), 0)

    def test_unknown_enrollment_returns_404(self):
        body = self.body()
        body["enrollment"] = 99999
        self.assertEqual(self.post(self.owner, body).status_code, 404)

    def test_second_plan_for_same_enrollment_returns_400(self):
        self.assertEqual(self.post(self.owner, self.body()).status_code, 201)
        self.assertEqual(self.post(self.owner, self.body()).status_code, 400)
        self.assertEqual(FeePlan.objects.count(), 1)

    def test_bad_totals_return_400(self):
        for total in ["0.00", "-1.00", "100.005", "abc", "100000000.00"]:
            response = self.post(self.owner, self.body(total=total))
            self.assertEqual(response.status_code, 400, total)
        self.assertEqual(FeePlan.objects.count(), 0)

    def test_missing_or_empty_due_dates_return_400(self):
        self.assertEqual(self.post(self.owner, self.body(dues=[])).status_code, 400)
        body = self.body()
        del body["due_dates"]
        self.assertEqual(self.post(self.owner, body).status_code, 400)
        self.assertEqual(FeePlan.objects.count(), 0)

    def test_parent_sees_only_own_plan(self):
        plan_a = create_fee_plan(
            enrollment=self.enr_a, total_amount="1000.00", due_dates=[date(2026, 11, 1)]
        )
        plan_b = create_fee_plan(
            enrollment=self.enr_b, total_amount="2000.00", due_dates=[date(2026, 11, 1)]
        )
        response = self.get(self.parent_a, reverse("feeplan-list"))
        self.assertEqual([p["student"] for p in response.data["results"]], ["Asha"])
        url = reverse("feeplan-detail", args=[plan_b.pk])
        self.assertEqual(self.get(self.parent_a, url).status_code, 404)
        url = reverse("feeplan-detail", args=[plan_a.pk])
        self.assertEqual(self.get(self.parent_a, url).status_code, 200)

    def test_teacher_is_forbidden_from_fee_plans(self):
        response = self.get(self.teacher, reverse("feeplan-list"))
        self.assertEqual(response.status_code, 403)

    def test_fee_plan_list_query_count_does_not_grow(self):
        create_fee_plan(
            enrollment=self.enr_a, total_amount="1000.00",
            due_dates=[date(2026, 11, 1), date(2026, 12, 1)],
        )
        url = reverse("feeplan-list")
        self.api.force_authenticate(user=self.owner)
        with CaptureQueriesContext(connection) as before:
            self.api.get(url)
        for i in range(5):
            student = Student.objects.create(full_name=f"Extra {i}")
            enr = Enrollment.objects.create(
                student=student, batch=self.batch, joined_on=date(2026, 6, 1)
            )
            create_fee_plan(
                enrollment=enr, total_amount="1000.00",
                due_dates=[date(2026, 11, 1), date(2026, 12, 1)],
            )
        with CaptureQueriesContext(connection) as after:
            self.api.get(url)
        self.assertEqual(len(before), len(after))

class ImportServiceTests(TestCase):
    HEADER = "full_name,phone,guardian_name,batch,joined_on"

    def setUp(self):
        teacher = User.objects.create_user(
            "t_imp", password="pw", role=User.Role.TEACHER
        )
        mk = Batch.objects.create
        self.jee = mk(name="JEE Morning", teacher=teacher, start_date=date(2026, 6, 1))
        self.neet = mk(name="NEET Evening", teacher=teacher, start_date=date(2026, 6, 1))
        mk(name="Old Batch", teacher=teacher, start_date=date(2025, 6, 1), is_active=False)
        mk(name="Twin", teacher=teacher, start_date=date(2026, 6, 1))
        mk(name="twin", teacher=teacher, start_date=date(2026, 6, 1))

    def make_csv(self, *lines, header=None):
        return "\n".join([header or self.HEADER, *lines]).encode("utf-8")

    def run_import(self, *lines, commit=False, header=None):
        return import_students(raw=self.make_csv(*lines, header=header), commit=commit)

    def test_preview_saves_nothing(self):
        result = self.run_import(
            "Asha Rao,9876543210,Mr Rao,JEE Morning,2026-06-01",
            "Ravi Kumar,9123456780,,NEET Evening,",
        )
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["committed"])
        self.assertEqual(result["new_students"], 2)
        self.assertEqual(result["new_enrollments"], 2)
        self.assertEqual(Student.objects.count(), 0)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_commit_saves_students_and_enrollments(self):
        result = self.run_import(
            "Asha Rao,9876543210,Mr Rao,JEE Morning,2026-06-15",
            "Ravi Kumar,9123456780,,NEET Evening,",
            commit=True,
        )
        self.assertTrue(result["committed"])
        asha = Student.objects.get(full_name="Asha Rao")
        self.assertEqual(asha.phone, "9876543210")
        self.assertEqual(asha.guardian_name, "Mr Rao")
        enrollment = Enrollment.objects.get(student=asha)
        self.assertEqual(enrollment.batch, self.jee)
        self.assertEqual(enrollment.joined_on, date(2026, 6, 15))
        self.assertEqual(Student.objects.count(), 2)
        self.assertEqual(Enrollment.objects.count(), 2)

    def test_phone_is_normalised(self):
        self.run_import(
            "Asha,98765-43210,,JEE Morning,",
            "Ravi,+91 91234 56780,,JEE Morning,",
            commit=True,
        )
        self.assertEqual(Student.objects.get(full_name="Asha").phone, "9876543210")
        self.assertEqual(Student.objects.get(full_name="Ravi").phone, "+919123456780")

    def test_headers_are_case_insensitive_and_trimmed(self):
        result = self.run_import(
            "Asha,9876543210,JEE Morning",
            header=" Full_Name , PHONE , Batch ",
            commit=True,
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(Student.objects.count(), 1)

    def test_blank_lines_are_skipped(self):
        result = self.run_import(
            "", "Asha,9876543210,,JEE Morning,", ",,,,", "", commit=True
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["rows"], 1)

    def test_joined_on_defaults_to_today(self):
        self.run_import("Asha,9876543210,,JEE Morning,", commit=True)
        self.assertEqual(Enrollment.objects.get().joined_on, timezone.localdate())

    def test_one_bad_row_saves_nothing_and_reports_line_number(self):
        result = self.run_import(
            "Asha,9876543210,,JEE Morning,",  # line 2
            "Ravi,9123456780,,NEET Evening,",  # line 3
            "Meera,9000000001,,No Such Batch,",  # line 4
            commit=True,
        )
        self.assertFalse(result["committed"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["row"], 4)
        self.assertEqual(result["errors"][0]["field"], "batch")
        self.assertEqual(Student.objects.count(), 0)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_each_kind_of_bad_row_is_reported_on_the_right_field(self):
        cases = [
            (",9876543210,,JEE Morning,", "full_name"),
            ("Asha,,,JEE Morning,", "phone"),
            ("Asha,12345,,JEE Morning,", "phone"),
            ("Asha,98765abcde,,JEE Morning,", "phone"),
            ("Asha,9876543210,,,", "batch"),
            ("Asha,9876543210,,Old Batch,", "batch"),
            ("Asha,9876543210,,Twin,", "batch"),
            ("Asha,9876543210,,JEE Morning,01/06/2026", "joined_on"),
            ("Asha,9876543210,,JEE Morning,2026-02-31", "joined_on"),
            ("A" * 151 + ",9876543210,,JEE Morning,", "full_name"),
        ]
        for line, field in cases:
            result = self.run_import(line)
            self.assertEqual(len(result["errors"]), 1, line)
            self.assertEqual(result["errors"][0]["field"], field, line)
            self.assertEqual(result["errors"][0]["row"], 2, line)
        self.assertEqual(Student.objects.count(), 0)

    def test_duplicate_row_in_file_is_rejected(self):
        result = self.run_import(
            "Asha,9876543210,,JEE Morning,",
            "asha,9876543210,,jee morning,",
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["row"], 3)

    def test_already_enrolled_student_is_rejected(self):
        asha = Student.objects.create(full_name="Asha", phone="9876543210")
        Enrollment.objects.create(
            student=asha, batch=self.jee, joined_on=date(2026, 6, 1)
        )
        result = self.run_import("Asha,9876543210,,JEE Morning,", commit=True)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["field"], "batch")
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_existing_student_is_reused_not_duplicated(self):
        asha = Student.objects.create(full_name="Asha", phone="9876543210")
        result = self.run_import("ASHA,9876543210,,NEET Evening,", commit=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["new_students"], 0)
        self.assertEqual(result["existing_students_matched"], 1)
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(asha.enrollments.get().batch, self.neet)

    def test_same_new_student_in_two_batches_is_created_once(self):
        result = self.run_import(
            "Asha,9876543210,,JEE Morning,",
            "Asha,9876543210,,NEET Evening,",
            commit=True,
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["new_students"], 1)
        self.assertEqual(result["new_enrollments"], 2)
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(Enrollment.objects.count(), 2)

    def test_ambiguous_existing_student_is_rejected(self):
        Student.objects.create(full_name="Asha", phone="9876543210")
        Student.objects.create(full_name="Asha", phone="9876543210")
        result = self.run_import("Asha,9876543210,,JEE Morning,")
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["field"], "full_name")

    def test_missing_required_column_rejects_whole_file(self):
        with self.assertRaises(ImportFileError):
            self.run_import("Asha,JEE Morning", header="full_name,batch")

    def test_empty_or_header_only_file_is_rejected(self):
        with self.assertRaises(ImportFileError):
            import_students(raw=b"", commit=False)
        with self.assertRaises(ImportFileError):
            self.run_import()  # header only, no data rows

    def test_non_utf8_file_is_rejected(self):
        with self.assertRaises(ImportFileError):
            import_students(raw=b"\xff\xfe full_name,phone,batch", commit=False)

    def test_excel_bom_is_accepted(self):
        raw = ("\ufeff" + self.HEADER + "\nAsha,9876543210,,JEE Morning,").encode("utf-8")
        result = import_students(raw=raw, commit=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(Student.objects.count(), 1)

    def test_too_many_rows_is_rejected(self):
        lines = [f"S{i},{9000000000 + i},,JEE Morning," for i in range(1001)]
        with self.assertRaises(ImportFileError):
            self.run_import(*lines)


class ImportApiTests(TestCase):
    CSV = (
        "full_name,phone,guardian_name,batch,joined_on\n"
        "Asha Rao,9876543210,Mr Rao,JEE Morning,2026-06-15\n"
        "Ravi Kumar,9123456780,,JEE Morning,\n"
    )

    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher = mk("tea", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        Batch.objects.create(
            name="JEE Morning", teacher=self.teacher, start_date=date(2026, 6, 1)
        )
        self.api = APIClient()
        self.url = reverse("import-students")

    def upload(self, user, content=None, commit="false"):
        self.api.force_authenticate(user=user)
        data = {"commit": commit}
        if content is not None:
            data["file"] = SimpleUploadedFile(
                "students.csv", content, content_type="text/csv"
            )
        return self.api.post(self.url, data, format="multipart")

    def test_owner_preview_saves_nothing(self):
        response = self.upload(self.owner, self.CSV.encode())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["committed"])
        self.assertEqual(response.data["new_students"], 2)
        self.assertEqual(Student.objects.count(), 0)

    def test_owner_commit_saves(self):
        response = self.upload(self.owner, self.CSV.encode(), commit="true")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["committed"])
        self.assertEqual(Student.objects.count(), 2)
        self.assertEqual(Enrollment.objects.count(), 2)

    def test_teacher_and_parent_are_forbidden(self):
        for user in (self.teacher, self.parent):
            response = self.upload(user, self.CSV.encode(), commit="true")
            self.assertEqual(response.status_code, 403)
        self.assertEqual(Student.objects.count(), 0)

    def test_row_errors_return_400_and_save_nothing(self):
        bad = self.CSV + "Meera,9000000001,,No Such Batch,\n"
        response = self.upload(self.owner, bad.encode(), commit="true")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"][0]["row"], 4)
        self.assertEqual(Student.objects.count(), 0)

    def test_missing_file_returns_400(self):
        self.assertEqual(self.upload(self.owner).status_code, 400)

    def test_unreadable_file_returns_400(self):
        response = self.upload(self.owner, b"name,batch\nAsha,JEE Morning\n")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Missing column", str(response.data["detail"]))

    def test_oversized_file_returns_400(self):
        big = b"full_name,phone,batch\n" + b"x" * 1_000_001
        self.assertEqual(self.upload(self.owner, big).status_code, 400)

class AuthApiTests(TestCase):
    def setUp(self):
        cache.clear()  # the login rate limit counts requests in the cache
        self.user = User.objects.create_user(
            "owner_x", password="correct-horse", role=User.Role.OWNER
        )
        self.api = APIClient()
        self.login_url = reverse("auth-login")

    def login(self, username="owner_x", password="correct-horse", client=None, **extra):
        client = client or self.api
        return client.post(
            self.login_url,
            {"username": username, "password": password},
            format="json",
            **extra,
        )

    def test_csrf_endpoint_sets_the_cookie(self):
        response = self.api.get(reverse("auth-csrf"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)

    def test_login_works_and_starts_a_session(self):
        self.assertEqual(self.api.get(reverse("me")).status_code, 403)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["username"], "owner_x")
        self.assertEqual(response.data["role"], "owner")
        self.assertEqual(self.api.get(reverse("me")).status_code, 200)

    def test_wrong_password_is_rejected(self):
        response = self.login(password="nope")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.api.get(reverse("me")).status_code, 403)

    def test_unknown_user_gets_the_same_message_as_wrong_password(self):
        ghost = self.login(username="ghost", password="x")
        wrong = self.login(password="nope")
        self.assertEqual(ghost.status_code, 400)
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(ghost.data, wrong.data)

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.login().status_code, 400)

    def test_missing_fields_return_400(self):
        self.assertEqual(self.api.post(self.login_url, {}, format="json").status_code, 400)
        response = self.api.post(
            self.login_url, {"username": "owner_x"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_logout_ends_the_session(self):
        self.login()
        self.assertEqual(self.api.post(reverse("auth-logout")).status_code, 204)
        self.assertEqual(self.api.get(reverse("me")).status_code, 403)

    def test_logout_when_nobody_is_logged_in_is_harmless(self):
        self.assertEqual(self.api.post(reverse("auth-logout")).status_code, 204)

    def test_login_needs_a_csrf_token(self):
        strict = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.login(client=strict).status_code, 403)
        strict.get(reverse("auth-csrf"))
        token = strict.cookies["csrftoken"].value
        response = self.login(client=strict, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_a_logged_in_post_needs_a_csrf_token(self):
        strict = APIClient(enforce_csrf_checks=True)
        strict.get(reverse("auth-csrf"))
        self.login(client=strict, HTTP_X_CSRFTOKEN=strict.cookies["csrftoken"].value)

        logout_url = reverse("auth-logout")
        self.assertEqual(strict.post(logout_url).status_code, 403)  # no token
        self.assertEqual(strict.get(reverse("me")).status_code, 200)  # still in

        fresh_token = strict.cookies["csrftoken"].value  # login rotated it
        response = strict.post(logout_url, HTTP_X_CSRFTOKEN=fresh_token)
        self.assertEqual(response.status_code, 204)

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.login(password="nope").status_code, 400)
        self.assertEqual(self.login(password="nope").status_code, 429)
        # even the correct password is refused until the limit resets
        self.assertEqual(self.login().status_code, 429)

class DetailSupportTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        teacher = mk("tea", password="pw", role=User.Role.TEACHER)
        self.parent_a = mk("pa", password="pw", role=User.Role.STUDENT)
        batch = Batch.objects.create(
            name="A", teacher=teacher, start_date=date(2026, 6, 1)
        )
        self.asha = Student.objects.create(full_name="Asha", user=self.parent_a)
        self.bala = Student.objects.create(full_name="Bala")
        enr_a = Enrollment.objects.create(
            student=self.asha, batch=batch, joined_on=date(2026, 6, 1)
        )
        enr_b = Enrollment.objects.create(
            student=self.bala, batch=batch, joined_on=date(2026, 6, 1)
        )
        self.plan_a = create_fee_plan(
            enrollment=enr_a, total_amount="10000.00", due_dates=[date(2026, 7, 1)]
        )
        self.plan_b = create_fee_plan(
            enrollment=enr_b, total_amount="20000.00", due_dates=[date(2026, 7, 1)]
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.owner)

    def pay(self, plan, amount):
        return record_payment(
            fee_plan=plan, amount=Decimal(amount), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.owner,
        )

    def test_reversed_payment_is_flagged(self):
        kept = self.pay(self.plan_a, "1000.00")
        wrong = self.pay(self.plan_a, "2000.00")
        reversal = reverse_payment(
            entry=wrong, reason="Mistake", received_by=self.owner
        )
        response = self.api.get(reverse("payment-list"), {"fee_plan": self.plan_a.pk})
        flags = {row["id"]: row["is_reversed"] for row in response.data["results"]}
        self.assertEqual(flags, {kept.pk: False, wrong.pk: True, reversal.pk: False})

    def test_new_payment_response_says_not_reversed(self):
        response = self.api.post(
            reverse("payment-list"),
            {
                "fee_plan": self.plan_a.pk,
                "amount": "500.00",
                "method": "cash",
                "paid_on": "2026-10-01",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIs(response.data["is_reversed"], False)

    def test_dues_can_be_filtered_to_one_plan(self):
        response = self.api.get(reverse("dues"), {"fee_plan": self.plan_b.pk})
        ids = [row["fee_plan_id"] for row in response.data["results"]]
        self.assertEqual(ids, [self.plan_b.pk])

    def test_dues_filter_cannot_expose_another_familys_plan(self):
        self.api.force_authenticate(user=self.parent_a)
        response = self.api.get(reverse("dues"), {"fee_plan": self.plan_b.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_odd_digit_filters_do_not_crash(self):
        for value in ["²", "٣", "9" * 40, "abc", "-1", ""]:
            for name in ("dues", "payment-list"):
                response = self.api.get(reverse(name), {"fee_plan": value})
                self.assertEqual(response.status_code, 200, (name, value))

class SplitSafetyTests(TestCase):
    def test_total_too_small_to_split_is_rejected(self):
        with self.assertRaises(LedgerError):
            split_amount("0.01", 2)
        self.assertEqual(split_amount("0.01", 1), [Decimal("0.01")])
        self.assertEqual(
            split_amount("0.02", 2), [Decimal("0.01"), Decimal("0.01")]
        )

    def test_every_installment_is_at_least_one_paisa(self):
        for total in ["0.12", "0.13", "1.00", "10000.00"]:
            for parts in range(1, 13):
                amounts = split_amount(total, parts)
                self.assertTrue(all(a >= Decimal("0.01") for a in amounts))
                self.assertEqual(sum(amounts), Decimal(total))

    def test_duplicate_due_dates_are_rejected(self):
        with self.assertRaises(LedgerError):
            preview_installments(
                total_amount="100.00",
                due_dates=[date(2026, 7, 1), date(2026, 7, 1)],
            )

    def test_preview_sorts_dates_and_saves_nothing(self):
        rows = preview_installments(
            total_amount="10000.00",
            due_dates=[date(2026, 9, 1), date(2026, 7, 1), date(2026, 8, 1)],
        )
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])
        self.assertEqual(
            [r["due_date"] for r in rows],
            [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)],
        )
        self.assertEqual(
            [r["amount"] for r in rows],
            [Decimal("3333.34"), Decimal("3333.33"), Decimal("3333.33")],
        )
        self.assertEqual(FeePlan.objects.count(), 0)


class OwnerScreensApiTests(TestCase):
    DUES = ["2026-12-01", "2026-10-01", "2026-11-01"]  # deliberately unordered

    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher = mk("tea", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        self.batch = Batch.objects.create(
            name="A", teacher=self.teacher, start_date=date(2026, 6, 1)
        )
        self.asha = Student.objects.create(full_name="Asha Rao")
        self.bala = Student.objects.create(full_name="Bala Singh")
        self.chetan = Student.objects.create(full_name="Chetan Das")
        self.enr_asha = Enrollment.objects.create(
            student=self.asha, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        self.enr_bala = Enrollment.objects.create(
            student=self.bala, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        self.enr_chetan = Enrollment.objects.create(
            student=self.chetan, batch=self.batch,
            joined_on=date(2026, 6, 1), is_active=False,
        )
        create_fee_plan(
            enrollment=self.enr_bala,
            total_amount="1000.00",
            due_dates=[date(2026, 11, 1)],
        )
        self.api = APIClient()

    def post(self, user, name, body):
        self.api.force_authenticate(user=user)
        return self.api.post(reverse(name), body, format="json")

    def get(self, user, params=None):
        self.api.force_authenticate(user=user)
        return self.api.get(reverse("enrollment-list"), params or {})

    def names(self, params):
        response = self.get(self.owner, params)
        return [row["student_name"] for row in response.data["results"]]

    def test_preview_returns_exact_split_and_saves_nothing(self):
        before = FeePlan.objects.count()
        response = self.post(
            self.owner,
            "feeplan-preview",
            {"total_amount": "10000.00", "due_dates": self.DUES},
        )
        self.assertEqual(response.status_code, 200)
        rows = response.data["installments"]
        self.assertEqual([r["amount"] for r in rows], ["3333.34", "3333.33", "3333.33"])
        self.assertEqual(
            [r["due_date"] for r in rows], ["2026-10-01", "2026-11-01", "2026-12-01"]
        )
        self.assertEqual(FeePlan.objects.count(), before)

    def test_preview_matches_what_create_saves(self):
        body = {"total_amount": "10000.00", "due_dates": self.DUES}
        preview = self.post(self.owner, "feeplan-preview", body).data["installments"]
        created = self.post(
            self.owner, "feeplan-list", {**body, "enrollment": self.enr_asha.pk}
        )
        self.assertEqual(created.status_code, 201)
        keys = ("number", "amount", "due_date")
        shown = [{k: row[k] for k in keys} for row in preview]
        saved = [{k: row[k] for k in keys} for row in created.data["installments"]]
        self.assertEqual(saved, shown)

    def test_preview_is_owner_only(self):
        body = {"total_amount": "100.00", "due_dates": self.DUES}
        for user in (self.teacher, self.parent):
            self.assertEqual(self.post(user, "feeplan-preview", body).status_code, 403)

    def test_preview_rejects_bad_input(self):
        cases = [
            {"total_amount": "0.00", "due_dates": self.DUES},
            {"total_amount": "100.005", "due_dates": self.DUES},
            {"total_amount": "abc", "due_dates": self.DUES},
            {"total_amount": "100.00", "due_dates": []},
            {"total_amount": "100.00", "due_dates": ["2026-10-01", "2026-10-01"]},
            {"total_amount": "0.01", "due_dates": ["2026-10-01", "2026-11-01"]},
            {"due_dates": self.DUES},
            {"total_amount": "100.00"},
        ]
        for body in cases:
            response = self.post(self.owner, "feeplan-preview", body)
            self.assertEqual(response.status_code, 400, body)

    def test_create_with_total_too_small_to_split_returns_400(self):
        body = {
            "enrollment": self.enr_asha.pk,
            "total_amount": "0.01",
            "due_dates": self.DUES,
        }
        response = self.post(self.owner, "feeplan-list", body)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FeePlan.objects.filter(enrollment=self.enr_asha).exists())

    def test_enrollments_are_owner_only(self):
        for user in (self.teacher, self.parent):
            self.assertEqual(self.get(user).status_code, 403)

    def test_enrollment_filters(self):
        rows = self.get(self.owner).data["results"]
        flags = {row["student_name"]: row["has_fee_plan"] for row in rows}
        self.assertEqual(
            flags, {"Asha Rao": False, "Bala Singh": True, "Chetan Das": False}
        )
        self.assertEqual(self.names({"without_plan": "1"}), ["Asha Rao", "Chetan Das"])
        self.assertEqual(self.names({"without_plan": "1", "active": "1"}), ["Asha Rao"])
        self.assertEqual(self.names({"search": "bala"}), ["Bala Singh"])
        self.assertEqual(
            self.names({"search": "  "}), ["Asha Rao", "Bala Singh", "Chetan Das"]
        )

    def test_enrollment_list_query_count_does_not_grow(self):
        url = reverse("enrollment-list")
        self.api.force_authenticate(user=self.owner)
        with CaptureQueriesContext(connection) as before:
            self.api.get(url)
        for i in range(5):
            student = Student.objects.create(full_name=f"Extra {i}")
            Enrollment.objects.create(
                student=student, batch=self.batch, joined_on=date(2026, 6, 1)
            )
        with CaptureQueriesContext(connection) as after:
            self.api.get(url)
        self.assertEqual(len(before), len(after))

    def test_very_long_search_does_not_crash(self):
        self.assertEqual(self.get(self.owner, {"search": "x" * 500}).status_code, 200)

class AttendanceReadApiTests(TestCase):
    TODAY = date(2026, 10, 30)
    D1, D2, D3 = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)

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
        # Asha is in both batches. Bala is only in batch B.
        for student, batch in [
            (self.asha, self.batch_a),
            (self.asha, self.batch_b),
            (self.bala, self.batch_b),
        ]:
            Enrollment.objects.create(
                student=student, batch=batch, joined_on=date(2026, 6, 1)
            )

        a, b = self.asha.pk, self.bala.pk
        self.mark(self.batch_a, self.D1, {a: "present"})
        self.mark(self.batch_a, self.D2, {a: "absent"})
        self.mark(self.batch_a, self.D3, {a: "late"})
        self.mark(self.batch_b, self.D1, {a: "leave", b: "present"})
        self.mark(self.batch_b, self.D2, {a: "present", b: "absent"})
        self.api = APIClient()

    # ---- helpers ----
    def mark(self, batch, day, statuses):
        return mark_attendance(
            batch=batch, date=day, statuses=statuses,
            marked_by=self.owner, today=self.TODAY,
        )

    def get(self, user, name, params=None):
        self.api.force_authenticate(user=user)
        return self.api.get(reverse(name), params or {})

    def records(self, user, params=None):
        response = self.get(user, "attendance-list", params)
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    # ---- who can see what ----
    def test_anonymous_user_is_rejected(self):
        self.assertEqual(self.get(None, "attendance-list").status_code, 403)
        self.assertEqual(self.get(None, "attendance-summary").status_code, 403)

    def test_parent_sees_only_own_childs_records(self):
        rows = self.records(self.parent_a)
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["student_name"] for row in rows}, {"Asha"})

    def test_other_parent_sees_only_own_records(self):
        rows = self.records(self.parent_b)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["student_name"] for row in rows}, {"Bala"})

    def test_teacher_sees_only_own_batch_records(self):
        rows = self.records(self.teacher_a)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["batch_name"] for row in rows}, {"A"})
        rows = self.records(self.teacher_b)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["batch_name"] for row in rows}, {"B"})

    def test_owner_sees_everything(self):
        self.assertEqual(len(self.records(self.owner)), 7)

    def test_parent_cannot_reach_another_child_with_a_filter(self):
        self.assertEqual(self.records(self.parent_a, {"student": self.bala.pk}), [])
        self.assertEqual(self.records(self.parent_a, {"batch": 99999}), [])

    # ---- filters ----
    def test_filters_narrow_the_list(self):
        self.assertEqual(len(self.records(self.owner, {"batch": self.batch_a.pk})), 3)
        self.assertEqual(len(self.records(self.owner, {"student": self.bala.pk})), 2)
        rows = self.records(self.parent_a, {"from": "2026-10-27", "to": "2026-10-27"})
        self.assertEqual({row["status"] for row in rows}, {"absent", "present"})
        self.assertEqual(len(rows), 2)
        rows = self.records(self.owner, {"from": "2026-10-28"})
        self.assertEqual(len(rows), 1)

    def test_bad_date_filters_return_400(self):
        for name in ("from", "to"):
            for value in ["abc", "2026-13-45", "2026-02-31", "²"]:
                response = self.get(self.owner, "attendance-list", {name: value})
                self.assertEqual(response.status_code, 400, (name, value))

    def test_bad_id_filters_do_not_crash(self):
        for name in ("student", "batch"):
            for value in ["²", "9" * 40, "abc", "-1", ""]:
                response = self.get(self.owner, "attendance-list", {name: value})
                self.assertEqual(response.status_code, 200, (name, value))

    def test_list_is_newest_first_and_carries_names(self):
        rows = self.records(self.owner)
        dates = [row["date"] for row in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))
        first = rows[0]
        self.assertEqual(first["date"], "2026-10-28")
        self.assertEqual(first["student_name"], "Asha")
        self.assertEqual(first["batch_name"], "A")
        self.assertEqual(first["batch"], self.batch_a.pk)
        self.assertEqual(first["status"], "late")

    # ---- summary ----
    def test_summary_counts_and_percentage(self):
        response = self.get(self.parent_a, "attendance-summary")
        self.assertEqual(response.status_code, 200)
        by_batch = {row["batch_name"]: row for row in response.data}
        self.assertEqual(set(by_batch), {"A", "B"})

        a = by_batch["A"]
        self.assertEqual(a["student_name"], "Asha")
        self.assertEqual(
            (a["total"], a["present"], a["absent"], a["late"], a["leave"]),
            (3, 1, 1, 1, 0),
        )
        self.assertEqual(a["percent"], 66.7)  # (present + late) / 3

        b = by_batch["B"]
        self.assertEqual(
            (b["total"], b["present"], b["absent"], b["late"], b["leave"]),
            (2, 1, 0, 0, 1),
        )
        self.assertEqual(b["percent"], 100.0)  # leave day is left out: 1 / 1

    def test_summary_is_scoped(self):
        rows = self.get(self.teacher_a, "attendance-summary").data
        self.assertEqual([(r["student_name"], r["batch_name"]) for r in rows], [("Asha", "A")])

        rows = self.get(self.parent_b, "attendance-summary").data
        self.assertEqual([(r["student_name"], r["batch_name"]) for r in rows], [("Bala", "B")])
        self.assertEqual(rows[0]["percent"], 50.0)

        rows = self.get(self.owner, "attendance-summary").data
        self.assertEqual(
            [(r["student_name"], r["batch_name"]) for r in rows],
            [("Asha", "A"), ("Asha", "B"), ("Bala", "B")],
        )

    def test_summary_percent_is_none_when_only_leave(self):
        dev = Student.objects.create(full_name="Dev")
        Enrollment.objects.create(
            student=dev, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        self.mark(
            self.batch_a, date(2026, 10, 19), {self.asha.pk: "present", dev.pk: "leave"}
        )
        rows = self.get(self.owner, "attendance-summary").data
        dev_row = next(row for row in rows if row["student_name"] == "Dev")
        self.assertEqual(dev_row["total"], 1)
        self.assertIsNone(dev_row["percent"])

    # ---- query counts and safety ----
    def count_queries_growth(self, name):
        self.api.force_authenticate(user=self.owner)
        url = reverse(name)
        with CaptureQueriesContext(connection) as before:
            self.api.get(url)
        for i in range(5):
            self.mark(self.batch_a, date(2026, 10, 20 + i), {self.asha.pk: "present"})
        with CaptureQueriesContext(connection) as after:
            self.api.get(url)
        return len(before), len(after)

    def test_list_query_count_does_not_grow(self):
        before, after = self.count_queries_growth("attendance-list")
        self.assertEqual(before, after)

    def test_summary_query_count_does_not_grow(self):
        before, after = self.count_queries_growth("attendance-summary")
        self.assertEqual(before, after)

    def test_attendance_endpoints_are_read_only(self):
        self.api.force_authenticate(user=self.owner)
        for name in ("attendance-list", "attendance-summary"):
            response = self.api.post(reverse(name), {"status": "present"}, format="json")
            self.assertEqual(response.status_code, 405, name)

class UserAdminApiTests(TestCase):
    STRONG = "Sturdy-Pass-7391"

    def setUp(self):
        cache.clear()  # the login rate limit counts requests in the cache
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher = mk("tea", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        self.api = APIClient()
        self.url = reverse("user-list")

    def post(self, user, body):
        self.api.force_authenticate(user=user)
        return self.api.post(self.url, body, format="json")

    def get(self, user, params=None):
        self.api.force_authenticate(user=user)
        return self.api.get(self.url, params or {})

    def body(self, **changes):
        data = {
            "username": "new.teacher",
            "first_name": "Nita",
            "role": "teacher",
            "password": self.STRONG,
        }
        data.update(changes)
        return data

    def test_only_the_owner_can_list_or_create_users(self):
        self.assertEqual(self.get(None).status_code, 403)
        for user in (self.teacher, self.parent):
            self.assertEqual(self.get(user).status_code, 403)
            self.assertEqual(self.post(user, self.body()).status_code, 403)
        self.assertFalse(User.objects.filter(username="new.teacher").exists())

    def test_owner_creates_a_teacher_who_can_log_in(self):
        response = self.post(self.owner, self.body())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["username"], "new.teacher")
        self.assertEqual(response.data["role"], "teacher")
        self.assertNotIn("password", response.data)

        user = User.objects.get(username="new.teacher")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertNotEqual(user.password, self.STRONG)  # stored as a hash
        self.assertTrue(user.check_password(self.STRONG))

        login = APIClient().post(
            reverse("auth-login"),
            {"username": "new.teacher", "password": self.STRONG},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.data["role"], "teacher")

    def test_owner_creates_a_parent_account(self):
        response = self.post(
            self.owner, self.body(username="parent.one", role="student")
        )
        self.assertEqual(response.status_code, 201)
        created = User.objects.get(username="parent.one")
        self.assertEqual(created.role, User.Role.STUDENT)

    def test_owner_accounts_cannot_be_created_here(self):
        for role in ("owner", "admin", ""):
            response = self.post(self.owner, self.body(role=role))
            self.assertEqual(response.status_code, 400, role)
        self.assertFalse(User.objects.filter(username="new.teacher").exists())

    def test_duplicate_usernames_are_rejected_ignoring_case(self):
        for name in ("tea", "TEA", "Tea"):
            response = self.post(self.owner, self.body(username=name))
            self.assertEqual(response.status_code, 400, name)
        self.assertEqual(User.objects.count(), 3)

    def test_bad_usernames_are_rejected(self):
        for name in ("", "has space", "x" * 151, "bad/name!"):
            response = self.post(self.owner, self.body(username=name))
            self.assertEqual(response.status_code, 400, name)
        self.assertEqual(User.objects.count(), 3)

    def test_weak_passwords_are_rejected(self):
        # These rely on AUTH_PASSWORD_VALIDATORS in settings.py (Django's default).
        for password in ("short", "12345678", "password", "new.teacher1"):
            response = self.post(self.owner, self.body(password=password))
            self.assertEqual(response.status_code, 400, password)
        self.assertFalse(User.objects.filter(username="new.teacher").exists())

    def test_missing_fields_return_400(self):
        for field in ("username", "role", "password"):
            body = self.body()
            del body[field]
            self.assertEqual(self.post(self.owner, body).status_code, 400, field)
        body = self.body()
        del body["first_name"]  # the name is optional
        self.assertEqual(self.post(self.owner, body).status_code, 201)

    def test_list_filters_and_passwords_are_never_shown(self):
        User.objects.create_user(
            "gone", password="pw", role=User.Role.TEACHER, is_active=False
        )
        User.objects.create_user("zed", password="pw", role=User.Role.STUDENT)

        everyone = self.get(self.owner).data["results"]
        self.assertEqual(
            [u["username"] for u in everyone], ["gone", "own", "par", "tea", "zed"]
        )
        for row in everyone:
            self.assertEqual(
                set(row), {"id", "username", "first_name", "role", "is_active"}
            )

        teachers = self.get(self.owner, {"role": "teacher"}).data["results"]
        self.assertEqual([u["username"] for u in teachers], ["gone", "tea"])
        active = self.get(self.owner, {"role": "teacher", "active": "1"}).data["results"]
        self.assertEqual([u["username"] for u in active], ["tea"])
        found = self.get(self.owner, {"search": "ZE"}).data["results"]
        self.assertEqual([u["username"] for u in found], ["zed"])

        odd = self.get(self.owner, {"role": "nonsense", "search": "x" * 500})
        self.assertEqual(odd.status_code, 200)

    def test_user_list_query_count_does_not_grow(self):
        self.api.force_authenticate(user=self.owner)
        with CaptureQueriesContext(connection) as before:
            self.api.get(self.url)
        for i in range(5):
            User.objects.create_user(
                f"extra{i}", password="pw", role=User.Role.STUDENT
            )
        with CaptureQueriesContext(connection) as after:
            self.api.get(self.url)
        self.assertEqual(len(before), len(after))


class BatchAdminApiTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher_a = mk("ta", password="pw", role=User.Role.TEACHER)
        self.teacher_b = mk("tb", password="pw", role=User.Role.TEACHER)
        self.retired = mk(
            "old", password="pw", role=User.Role.TEACHER, is_active=False
        )
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        self.api = APIClient()

    def post(self, user, body):
        self.api.force_authenticate(user=user)
        return self.api.post(reverse("batch-list"), body, format="json")

    def patch(self, user, batch, body):
        self.api.force_authenticate(user=user)
        url = reverse("batch-detail", args=[batch.pk])
        return self.api.patch(url, body, format="json")

    def get(self, user, name, *args):
        self.api.force_authenticate(user=user)
        return self.api.get(reverse(name, args=list(args)))

    def body(self, **changes):
        data = {
            "name": "JEE Morning",
            "teacher": self.teacher_a.pk,
            "start_date": "2026-11-02",
        }
        data.update(changes)
        return data

    def make_batch(self, name="Existing", teacher=None):
        return Batch.objects.create(
            name=name, teacher=teacher or self.teacher_a, start_date=date(2026, 6, 1)
        )

    def test_owner_creates_a_batch(self):
        response = self.post(self.owner, self.body())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "JEE Morning")
        self.assertEqual(response.data["teacher"], self.teacher_a.pk)
        self.assertEqual(response.data["teacher_name"], "ta")
        self.assertTrue(response.data["is_active"])
        batch = Batch.objects.get()
        self.assertEqual(batch.teacher, self.teacher_a)
        self.assertEqual(batch.start_date, date(2026, 11, 2))

    def test_only_the_owner_can_create_or_change_batches(self):
        batch = self.make_batch()
        for user in (self.teacher_a, self.parent):
            self.assertEqual(self.post(user, self.body(name="Other")).status_code, 403)
            response = self.patch(user, batch, {"name": "Renamed"})
            self.assertEqual(response.status_code, 403)
        batch.refresh_from_db()
        self.assertEqual(batch.name, "Existing")
        self.assertEqual(Batch.objects.count(), 1)

    def test_batch_teacher_must_be_an_active_teacher(self):
        for teacher in (self.owner.pk, self.parent.pk, self.retired.pk, 99999):
            response = self.post(self.owner, self.body(teacher=teacher))
            self.assertEqual(response.status_code, 400, teacher)
        self.assertEqual(Batch.objects.count(), 0)

    def test_batch_names_are_trimmed_and_unique_ignoring_case(self):
        self.make_batch("Twin Name")
        for name in ("Twin Name", "TWIN NAME", "  twin name  "):
            response = self.post(self.owner, self.body(name=name))
            self.assertEqual(response.status_code, 400, name)
        self.assertEqual(Batch.objects.count(), 1)
        response = self.post(self.owner, self.body(name="  NEET Evening  "))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "NEET Evening")

    def test_required_fields_and_bad_values_return_400(self):
        for field in ("name", "teacher", "start_date"):
            body = self.body()
            del body[field]
            self.assertEqual(self.post(self.owner, body).status_code, 400, field)
        bad = [
            {"name": "   "},
            {"name": ""},
            {"name": "x" * 101},
            {"start_date": "02/11/2026"},
            {"start_date": "2026-02-31"},
        ]
        for changes in bad:
            response = self.post(self.owner, self.body(**changes))
            self.assertEqual(response.status_code, 400, changes)
        self.assertEqual(Batch.objects.count(), 0)

    def test_owner_edits_a_batch(self):
        batch = self.make_batch("Old Name")
        other = self.make_batch("Other", teacher=self.teacher_b)

        response = self.patch(self.owner, batch, {"name": "New Name"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "New Name")
        # changing only the capital letters of its own name is allowed
        self.assertEqual(
            self.patch(self.owner, batch, {"name": "NEW NAME"}).status_code, 200
        )
        # another batch's name is not
        self.assertEqual(
            self.patch(self.owner, batch, {"name": "other"}).status_code, 400
        )
        response = self.patch(
            self.owner, batch, {"is_active": False, "start_date": "2026-12-01"}
        )
        self.assertEqual(response.status_code, 200)

        batch.refresh_from_db()
        self.assertEqual(batch.name, "NEW NAME")
        self.assertFalse(batch.is_active)
        self.assertEqual(batch.start_date, date(2026, 12, 1))
        other.refresh_from_db()
        self.assertEqual(other.name, "Other")
        self.assertEqual(other.teacher, self.teacher_b)

        self.api.force_authenticate(user=self.owner)
        missing = self.api.patch(
            reverse("batch-detail", args=[99999]), {}, format="json"
        )
        self.assertEqual(missing.status_code, 404)

    def test_reassigning_a_batch_moves_access_between_teachers(self):
        batch = self.make_batch("Moving", teacher=self.teacher_a)
        self.assertEqual(self.get(self.teacher_a, "batch-detail", batch.pk).status_code, 200)
        self.assertEqual(self.get(self.teacher_b, "batch-detail", batch.pk).status_code, 404)

        # a retired teacher cannot be assigned
        response = self.patch(self.owner, batch, {"teacher": self.retired.pk})
        self.assertEqual(response.status_code, 400)

        response = self.patch(self.owner, batch, {"teacher": self.teacher_b.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["teacher_name"], "tb")

        self.assertEqual(self.get(self.teacher_a, "batch-detail", batch.pk).status_code, 404)
        self.assertEqual(self.get(self.teacher_b, "batch-detail", batch.pk).status_code, 200)

    def test_a_batch_whose_teacher_retired_can_still_be_renamed(self):
        batch = self.make_batch("Legacy", teacher=self.retired)
        response = self.patch(self.owner, batch, {"name": "Legacy 2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["teacher_name"], "old")

    def test_new_batch_is_usable_for_attendance_by_its_teacher(self):
        created = self.post(
            self.owner, self.body(name="Fresh", teacher=self.teacher_b.pk)
        )
        batch_id = created.data["id"]

        listing = self.get(self.teacher_b, "batch-list")
        self.assertEqual([b["name"] for b in listing.data["results"]], ["Fresh"])
        roster = self.get(self.teacher_b, "batch-attendance", batch_id)
        self.assertEqual(roster.status_code, 200)
        self.assertEqual(roster.data["records"], [])
        self.assertEqual(
            self.get(self.teacher_a, "batch-attendance", batch_id).status_code, 404
        )

class StudentAdminApiTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher_a = mk("ta", password="pw", role=User.Role.TEACHER)
        self.teacher_b = mk("tb", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        self.parent2 = mk("par2", password="pw", role=User.Role.STUDENT)
        self.retired_parent = mk(
            "oldpar", password="pw", role=User.Role.STUDENT, is_active=False
        )
        mkb = Batch.objects.create
        self.batch_a = mkb(name="A", teacher=self.teacher_a, start_date=date(2026, 6, 1))
        self.batch_b = mkb(name="B", teacher=self.teacher_b, start_date=date(2026, 6, 1))
        self.closed = mkb(
            name="Closed", teacher=self.teacher_a,
            start_date=date(2025, 6, 1), is_active=False,
        )
        self.api = APIClient()

    # ---- helpers ----
    def post(self, user, body):
        self.api.force_authenticate(user=user)
        return self.api.post(reverse("student-list"), body, format="json")

    def patch(self, user, student, body):
        self.api.force_authenticate(user=user)
        url = reverse("student-detail", args=[student.pk])
        return self.api.patch(url, body, format="json")

    def get(self, user, params=None):
        self.api.force_authenticate(user=user)
        return self.api.get(reverse("student-list"), params or {})

    def names(self, user, params=None):
        return [row["full_name"] for row in self.get(user, params).data["results"]]

    def body(self, **changes):
        data = {
            "full_name": "Asha Rao",
            "phone": "9876543210",
            "guardian_name": "Mr Rao",
        }
        data.update(changes)
        return data

    def make_student(self, name="Existing", phone="9000000001", **extra):
        return Student.objects.create(full_name=name, phone=phone, **extra)

    def enrol(self, student, batch, active=True):
        return Enrollment.objects.create(
            student=student, batch=batch, joined_on=date(2026, 6, 1), is_active=active
        )

    def make_people(self):
        self.asha = self.make_student("Asha", "9000000001", user=self.parent)
        self.bala = self.make_student("Bala", "9000000002")
        self.chetan = self.make_student("Chetan", "9000000003", is_active=False)
        self.dev = self.make_student("Dev", "9000000004")
        self.enrol(self.asha, self.batch_a)
        self.enrol(self.asha, self.batch_b)
        self.enrol(self.bala, self.batch_b)
        self.enrol(self.dev, self.batch_a)

    # ---- permissions ----
    def test_only_the_owner_can_add_or_edit_students(self):
        student = self.make_student()
        self.assertEqual(self.post(None, self.body()).status_code, 403)
        for user in (self.teacher_a, self.parent):
            self.assertEqual(self.post(user, self.body()).status_code, 403)
            response = self.patch(user, student, {"full_name": "Changed"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(Student.objects.count(), 1)
        student.refresh_from_db()
        self.assertEqual(student.full_name, "Existing")

    # ---- adding ----
    def test_owner_adds_a_student(self):
        response = self.post(self.owner, self.body(phone="98765-43210"))
        self.assertEqual(response.status_code, 201)
        data = response.data
        self.assertEqual(data["full_name"], "Asha Rao")
        self.assertEqual(data["phone"], "9876543210")  # tidied up
        self.assertEqual(data["guardian_name"], "Mr Rao")
        self.assertIsNone(data["user"])
        self.assertIsNone(data["parent_name"])
        self.assertEqual(data["enrollments"], [])
        self.assertTrue(data["is_active"])
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(Enrollment.objects.count(), 0)

    def test_a_student_can_be_enrolled_and_linked_to_a_parent_in_one_step(self):
        response = self.post(
            self.owner,
            self.body(batch=self.batch_a.pk, joined_on="2026-09-15", user=self.parent.pk),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["user"], self.parent.pk)
        self.assertEqual(response.data["parent_name"], "par")
        rows = response.data["enrollments"]
        self.assertEqual(
            [(r["batch"], r["batch_name"], r["joined_on"], r["is_active"]) for r in rows],
            [(self.batch_a.pk, "A", "2026-09-15", True)],
        )
        self.assertEqual(Student.objects.get().user, self.parent)

    def test_enrolment_date_defaults_to_today(self):
        self.post(self.owner, self.body(batch=self.batch_a.pk))
        self.assertEqual(Enrollment.objects.get().joined_on, timezone.localdate())

    def test_phone_is_required_and_must_look_like_a_phone_number(self):
        for phone in ["", "12345", "98765abcde", "+", "9" * 14, "9" * 21]:
            response = self.post(self.owner, self.body(phone=phone))
            self.assertEqual(response.status_code, 400, phone)
        body = self.body()
        del body["phone"]
        self.assertEqual(self.post(self.owner, body).status_code, 400)
        self.assertEqual(Student.objects.count(), 0)

    def test_name_is_required_and_limited(self):
        for name in ["", "   ", "x" * 151]:
            response = self.post(self.owner, self.body(full_name=name))
            self.assertEqual(response.status_code, 400, name)
        body = self.body()
        del body["full_name"]
        self.assertEqual(self.post(self.owner, body).status_code, 400)
        self.assertEqual(Student.objects.count(), 0)

    def test_duplicate_name_and_phone_is_rejected_ignoring_case(self):
        self.make_student("Asha Rao", "9876543210")
        for name in ("Asha Rao", "ASHA RAO", "  asha rao "):
            response = self.post(self.owner, self.body(full_name=name))
            self.assertEqual(response.status_code, 400, name)
        # the same number written with a space is the same number
        response = self.post(self.owner, self.body(phone="98765 43210"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Student.objects.count(), 1)
        # two different people can share a name
        response = self.post(self.owner, self.body(phone="9876543211"))
        self.assertEqual(response.status_code, 201)

    def test_parent_link_must_be_an_active_parent_account(self):
        for user_id in (self.teacher_a.pk, self.owner.pk, self.retired_parent.pk, 99999):
            response = self.post(self.owner, self.body(user=user_id))
            self.assertEqual(response.status_code, 400, user_id)
        self.assertEqual(Student.objects.count(), 0)

    def test_joining_date_needs_a_batch(self):
        response = self.post(self.owner, self.body(joined_on="2026-09-01"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Student.objects.count(), 0)

    def test_a_closed_or_unknown_batch_leaves_nothing_behind(self):
        for batch in (self.closed.pk, 99999):
            response = self.post(self.owner, self.body(batch=batch))
            self.assertEqual(response.status_code, 400, batch)
        self.assertEqual(Student.objects.count(), 0)
        self.assertEqual(Enrollment.objects.count(), 0)

    # ---- editing ----
    def test_owner_edits_a_student(self):
        student = self.make_student("Old Name", "9000000001", guardian_name="G")
        response = self.patch(
            self.owner,
            student,
            {"full_name": "New Name", "guardian_name": "New G", "phone": "90000 00002"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["full_name"], "New Name")
        self.assertEqual(response.data["phone"], "9000000002")
        student.refresh_from_db()
        self.assertEqual(
            (student.full_name, student.guardian_name, student.phone),
            ("New Name", "New G", "9000000002"),
        )
        # a bad phone is refused and changes nothing
        self.assertEqual(self.patch(self.owner, student, {"phone": "123"}).status_code, 400)
        student.refresh_from_db()
        self.assertEqual(student.phone, "9000000002")
        self.api.force_authenticate(user=self.owner)
        missing = self.api.patch(
            reverse("student-detail", args=[99999]), {}, format="json"
        )
        self.assertEqual(missing.status_code, 404)

    def test_editing_checks_for_clashes_only_when_name_or_phone_change(self):
        a = self.make_student("Asha", "9000000001")
        b = self.make_student("Bala", "9000000002")
        response = self.patch(
            self.owner, b, {"full_name": "asha", "phone": "9000000001"}
        )
        self.assertEqual(response.status_code, 400)
        # changing only the capital letters of its own name is fine
        self.assertEqual(
            self.patch(self.owner, a, {"full_name": "ASHA"}).status_code, 200
        )
        # old data may already hold a duplicate pair. Editing other fields still works.
        Student.objects.create(full_name="asha", phone="9000000001")
        self.assertEqual(
            self.patch(self.owner, a, {"guardian_name": "Mr A"}).status_code, 200
        )

    def test_parent_link_can_be_changed_and_removed(self):
        student = self.make_student(user=self.parent)
        response = self.patch(self.owner, student, {"user": self.parent2.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["parent_name"], "par2")
        student.refresh_from_db()
        self.assertEqual(student.user, self.parent2)

        response = self.patch(self.owner, student, {"user": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["user"])
        self.assertIsNone(response.data["parent_name"])
        student.refresh_from_db()
        self.assertIsNone(student.user)

        for user_id in (self.retired_parent.pk, self.teacher_a.pk):
            response = self.patch(self.owner, student, {"user": user_id})
            self.assertEqual(response.status_code, 400, user_id)

    def test_deactivating_a_student_ends_their_enrolments(self):
        student = self.make_student("Leaver", "9000000005")
        other = self.make_student("Stayer", "9000000006")
        e1 = self.enrol(student, self.batch_a)
        e2 = self.enrol(student, self.batch_b)
        e3 = self.enrol(other, self.batch_a)

        response = self.patch(self.owner, student, {"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual([e["is_active"] for e in response.data["enrollments"]], [False, False])
        for enrollment in (e1, e2):
            enrollment.refresh_from_db()
            self.assertFalse(enrollment.is_active)
        e3.refresh_from_db()
        self.assertTrue(e3.is_active)

        # the leaver is gone from the teacher's attendance sheet
        self.api.force_authenticate(user=self.teacher_a)
        roster = self.api.get(reverse("batch-attendance", args=[self.batch_a.pk]))
        self.assertEqual([r["student_name"] for r in roster.data["records"]], ["Stayer"])

        # reactivating the student does not silently bring the enrolments back
        self.assertEqual(
            self.patch(self.owner, student, {"is_active": True}).status_code, 200
        )
        e1.refresh_from_db()
        self.assertFalse(e1.is_active)

    def test_students_cannot_be_replaced_or_deleted(self):
        student = self.make_student()
        self.api.force_authenticate(user=self.owner)
        url = reverse("student-detail", args=[student.pk])
        self.assertEqual(self.api.put(url, self.body(), format="json").status_code, 405)
        self.assertEqual(self.api.delete(url).status_code, 405)
        self.assertTrue(Student.objects.filter(pk=student.pk).exists())

    # ---- listing ----
    def test_owner_list_has_enrolments_and_filters(self):
        self.make_people()
        rows = self.get(self.owner).data["results"]
        by_name = {row["full_name"]: row for row in rows}
        self.assertEqual(
            set(by_name["Asha"]),
            {"id", "full_name", "phone", "guardian_name", "user",
             "parent_name", "is_active", "enrollments"},
        )
        self.assertEqual([e["batch_name"] for e in by_name["Asha"]["enrollments"]], ["A", "B"])
        self.assertEqual(by_name["Asha"]["parent_name"], "par")
        self.assertIsNone(by_name["Bala"]["user"])

        owner = self.owner
        self.assertEqual(self.names(owner), ["Asha", "Bala", "Chetan", "Dev"])
        self.assertEqual(self.names(owner, {"search": "ala"}), ["Bala"])
        self.assertEqual(self.names(owner, {"search": "0003"}), ["Chetan"])  # by phone
        self.assertEqual(self.names(owner, {"active": "1"}), ["Asha", "Bala", "Dev"])
        self.assertEqual(self.names(owner, {"batch": self.batch_a.pk}), ["Asha", "Dev"])
        self.assertEqual(self.names(owner, {"batch": self.batch_b.pk}), ["Asha", "Bala"])
        self.assertEqual(
            self.names(owner, {"without_parent": "1"}), ["Bala", "Chetan", "Dev"]
        )
        self.assertEqual(
            self.names(owner, {"batch": self.batch_b.pk, "without_parent": "1"}), ["Bala"]
        )

    def test_teachers_and_parents_keep_the_basic_view(self):
        self.make_people()
        rows = self.get(self.teacher_a).data["results"]
        self.assertEqual([r["full_name"] for r in rows], ["Asha", "Dev"])
        basic = {"id", "full_name", "phone", "guardian_name", "is_active"}
        self.assertEqual(set(rows[0]), basic)
        # the owner-only filters are ignored, so a teacher cannot probe other batches
        self.assertEqual(
            self.names(self.teacher_a, {"batch": self.batch_b.pk}), ["Asha", "Dev"]
        )
        self.assertEqual(
            self.names(self.teacher_a, {"without_parent": "1"}), ["Asha", "Dev"]
        )
        rows = self.get(self.parent).data["results"]
        self.assertEqual([r["full_name"] for r in rows], ["Asha"])
        self.assertEqual(set(rows[0]), basic)

    def test_owner_list_query_count_does_not_grow(self):
        self.make_people()
        self.api.force_authenticate(user=self.owner)
        url = reverse("student-list")
        with CaptureQueriesContext(connection) as before:
            self.api.get(url)
        for i in range(5):
            student = self.make_student(f"Extra {i}", f"90000001{i:02d}", user=self.parent2)
            self.enrol(student, self.batch_a)
            self.enrol(student, self.batch_b)
        with CaptureQueriesContext(connection) as after:
            self.api.get(url)
        self.assertEqual(len(before), len(after))

    def test_odd_filter_values_do_not_crash(self):
        self.make_people()
        for name in ("search", "batch", "active", "without_parent"):
            for value in ["²", "9" * 40, "abc", "-1", "", " ", "x" * 500]:
                response = self.get(self.owner, {name: value})
                self.assertEqual(response.status_code, 200, (name, value))


class EnrollmentWriteApiTests(TestCase):
    def setUp(self):
        mk = User.objects.create_user
        self.owner = mk("own", password="pw", role=User.Role.OWNER)
        self.teacher_a = mk("ta", password="pw", role=User.Role.TEACHER)
        self.teacher_b = mk("tb", password="pw", role=User.Role.TEACHER)
        self.parent = mk("par", password="pw", role=User.Role.STUDENT)
        mkb = Batch.objects.create
        self.batch_a = mkb(name="A", teacher=self.teacher_a, start_date=date(2026, 6, 1))
        self.batch_b = mkb(name="B", teacher=self.teacher_b, start_date=date(2026, 6, 1))
        self.closed = mkb(
            name="Closed", teacher=self.teacher_a,
            start_date=date(2025, 6, 1), is_active=False,
        )
        mks = Student.objects.create
        self.asha = mks(full_name="Asha", phone="9000000001", user=self.parent)
        self.bala = mks(full_name="Bala", phone="9000000002")
        self.gone = mks(full_name="Gone", phone="9000000003", is_active=False)
        self.enr_asha_a = Enrollment.objects.create(
            student=self.asha, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )
        self.api = APIClient()

    def post(self, user, body):
        self.api.force_authenticate(user=user)
        return self.api.post(reverse("enrollment-list"), body, format="json")

    def patch(self, user, enrollment, body):
        self.api.force_authenticate(user=user)
        url = reverse("enrollment-detail", args=[enrollment.pk])
        return self.api.patch(url, body, format="json")

    def get(self, user, name, *args):
        self.api.force_authenticate(user=user)
        return self.api.get(reverse(name, args=list(args)))

    def test_only_the_owner_can_enrol_or_change_enrolments(self):
        body = {"student": self.bala.pk, "batch": self.batch_a.pk}
        self.assertEqual(self.post(None, body).status_code, 403)
        for user in (self.teacher_a, self.parent):
            self.assertEqual(self.post(user, body).status_code, 403)
            response = self.patch(user, self.enr_asha_a, {"is_active": False})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(Enrollment.objects.count(), 1)
        self.enr_asha_a.refresh_from_db()
        self.assertTrue(self.enr_asha_a.is_active)

    def test_owner_enrols_an_existing_student_and_access_follows(self):
        response = self.post(
            self.owner,
            {"student": self.asha.pk, "batch": self.batch_b.pk, "joined_on": "2026-10-20"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["student_name"], "Asha")
        self.assertEqual(response.data["batch_name"], "B")
        self.assertEqual(response.data["joined_on"], "2026-10-20")
        self.assertTrue(response.data["is_active"])
        self.assertFalse(response.data["has_fee_plan"])
        self.assertEqual(Enrollment.objects.count(), 2)

        # the other batch's teacher can now see the student
        listing = self.get(self.teacher_b, "student-list")
        self.assertEqual([r["full_name"] for r in listing.data["results"]], ["Asha"])
        # and the parent now sees both batches
        batches = self.get(self.parent, "batch-list")
        self.assertEqual([b["name"] for b in batches.data["results"]], ["A", "B"])

    def test_enrolment_date_defaults_to_today(self):
        response = self.post(
            self.owner, {"student": self.bala.pk, "batch": self.batch_a.pk}
        )
        self.assertEqual(response.status_code, 201)
        enrollment = Enrollment.objects.get(student=self.bala)
        self.assertEqual(enrollment.joined_on, timezone.localdate())

    def test_duplicate_enrolments_are_refused(self):
        body = {"student": self.asha.pk, "batch": self.batch_a.pk}
        response = self.post(self.owner, body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("already enrolled", str(response.data["detail"]))
        # after the student leaves the batch, a new row is refused. Reactivate instead.
        self.patch(self.owner, self.enr_asha_a, {"is_active": False})
        response = self.post(self.owner, body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Reactivate", str(response.data["detail"]))
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_bad_input_is_refused(self):
        cases = [
            {"student": self.gone.pk, "batch": self.batch_a.pk},  # inactive student
            {"student": self.bala.pk, "batch": self.closed.pk},  # inactive batch
            {"student": 99999, "batch": self.batch_a.pk},
            {"student": self.bala.pk, "batch": 99999},
            {"batch": self.batch_a.pk},
            {"student": self.bala.pk},
            {"student": self.bala.pk, "batch": self.batch_a.pk, "joined_on": "01/06/2026"},
            {"student": self.bala.pk, "batch": self.batch_a.pk, "joined_on": "2026-02-31"},
        ]
        for body in cases:
            self.assertEqual(self.post(self.owner, body).status_code, 400, body)
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_owner_can_end_and_restore_an_enrolment(self):
        Enrollment.objects.create(
            student=self.bala, batch=self.batch_a, joined_on=date(2026, 6, 1)
        )

        def roster():
            data = self.get(self.teacher_a, "batch-attendance", self.batch_a.pk).data
            return [r["student_name"] for r in data["records"]]

        self.assertEqual(roster(), ["Asha", "Bala"])
        response = self.patch(self.owner, self.enr_asha_a, {"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(response.data["student_name"], "Asha")
        self.assertEqual(roster(), ["Bala"])

        # the owner still sees it, unless asking for active ones only
        self.api.force_authenticate(user=self.owner)
        url = reverse("enrollment-list")
        listed = self.api.get(url, {"search": "Asha"})
        self.assertEqual([r["is_active"] for r in listed.data["results"]], [False])
        listed = self.api.get(url, {"search": "Asha", "active": "1"})
        self.assertEqual(listed.data["results"], [])

        response = self.patch(self.owner, self.enr_asha_a, {"is_active": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(roster(), ["Asha", "Bala"])

    def test_restoring_needs_an_active_student_and_batch(self):
        gone_enr = Enrollment.objects.create(
            student=self.gone, batch=self.batch_a,
            joined_on=date(2026, 6, 1), is_active=False,
        )
        closed_enr = Enrollment.objects.create(
            student=self.bala, batch=self.closed,
            joined_on=date(2026, 6, 1), is_active=False,
        )
        for enrollment in (gone_enr, closed_enr):
            response = self.patch(self.owner, enrollment, {"is_active": True})
            self.assertEqual(response.status_code, 400)
            enrollment.refresh_from_db()
            self.assertFalse(enrollment.is_active)

        # ending an enrolment is always allowed, even after its batch closed
        batch_c = Batch.objects.create(
            name="C", teacher=self.teacher_a, start_date=date(2026, 6, 1)
        )
        enr_c = Enrollment.objects.create(
            student=self.bala, batch=batch_c, joined_on=date(2026, 6, 1)
        )
        batch_c.is_active = False
        batch_c.save()
        self.assertEqual(
            self.patch(self.owner, enr_c, {"is_active": False}).status_code, 200
        )

    def test_bad_patch_bodies_are_refused(self):
        for body in ({}, {"is_active": "maybe"}):
            response = self.patch(self.owner, self.enr_asha_a, body)
            self.assertEqual(response.status_code, 400, body)
        self.enr_asha_a.refresh_from_db()
        self.assertTrue(self.enr_asha_a.is_active)

    def test_enrolments_cannot_be_replaced_or_deleted_and_missing_ones_are_404(self):
        self.api.force_authenticate(user=self.owner)
        url = reverse("enrollment-detail", args=[self.enr_asha_a.pk])
        put = self.api.put(url, {"is_active": False}, format="json")
        self.assertEqual(put.status_code, 405)
        self.assertEqual(self.api.delete(url).status_code, 405)
        self.assertTrue(Enrollment.objects.filter(pk=self.enr_asha_a.pk).exists())
        missing = self.api.patch(
            reverse("enrollment-detail", args=[99999]),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(missing.status_code, 404)

    def test_has_fee_plan_flag_survives_ending_an_enrolment(self):
        create_fee_plan(
            enrollment=self.enr_asha_a,
            total_amount="1000.00",
            due_dates=[date(2026, 11, 1)],
        )
        response = self.patch(self.owner, self.enr_asha_a, {"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_fee_plan"])


class ImportInactiveStudentTests(TestCase):
    def test_inactive_existing_student_is_rejected(self):
        teacher = User.objects.create_user(
            "t_inact", password="pw", role=User.Role.TEACHER
        )
        Batch.objects.create(
            name="JEE Morning", teacher=teacher, start_date=date(2026, 6, 1)
        )
        Student.objects.create(
            full_name="Asha", phone="9876543210", is_active=False
        )
        raw = b"full_name,phone,batch\nAsha,9876543210,JEE Morning\n"
        result = import_students(raw=raw, commit=True)
        self.assertFalse(result["committed"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["field"], "full_name")
        self.assertEqual(Enrollment.objects.count(), 0)

class AdminLockdownTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", password="pw")
        teacher = User.objects.create_user(
            "t_admin", password="pw", role=User.Role.TEACHER
        )
        self.batch = Batch.objects.create(
            name="A", teacher=teacher, start_date=date(2026, 6, 1)
        )
        self.student = Student.objects.create(full_name="Asha", phone="9000000001")
        enrollment = Enrollment.objects.create(
            student=self.student, batch=self.batch, joined_on=date(2026, 6, 1)
        )
        self.plan = create_fee_plan(
            enrollment=enrollment,
            total_amount="3000.00",
            due_dates=[date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)],
        )
        self.payment = record_payment(
            fee_plan=self.plan, amount=Decimal("500.00"), method="cash",
            paid_on=date(2026, 7, 1), received_by=self.root,
        )
        mark_attendance(
            batch=self.batch, date=date(2026, 10, 30),
            statuses={self.student.pk: "present"}, marked_by=self.root,
            today=date(2026, 10, 31),
        )
        self.client.force_login(self.root)

    def test_fee_plans_cannot_be_added_changed_or_deleted(self):
        add = reverse("admin:core_feeplan_add")
        change = reverse("admin:core_feeplan_change", args=[self.plan.pk])
        delete = reverse("admin:core_feeplan_delete", args=[self.plan.pk])
        self.assertEqual(self.client.get(add).status_code, 403)
        self.assertEqual(self.client.get(change).status_code, 200)  # looking is fine
        self.assertEqual(
            self.client.post(change, {"total_amount": "1.00"}).status_code, 403
        )
        self.assertEqual(self.client.get(delete).status_code, 403)
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.total_amount, Decimal("3000.00"))
        self.assertEqual(FeePlan.objects.count(), 1)

    def test_installments_cannot_be_edited_through_the_plan_page(self):
        first = self.plan.installments.get(number=1)
        data = {
            "total_amount": "3000.00",
            "installments-TOTAL_FORMS": "1",
            "installments-INITIAL_FORMS": "1",
            "installments-0-id": first.pk,
            "installments-0-fee_plan": self.plan.pk,
            "installments-0-amount": "1.00",
            "installments-0-due_date": "2026-07-01",
        }
        change = reverse("admin:core_feeplan_change", args=[self.plan.pk])
        self.assertEqual(self.client.post(change, data).status_code, 403)
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("1000.00"))
        self.assertEqual(Installment.objects.count(), 3)

    def test_ledger_and_attendance_cannot_be_added_in_the_admin(self):
        for name in (
            "admin:core_ledgerentry_add",
            "admin:core_attendancesession_add",
            "admin:core_attendancechange_add",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(AttendanceSession.objects.count(), 1)

    def test_ledger_entries_cannot_be_changed_or_deleted_and_lists_load(self):
        change = reverse("admin:core_ledgerentry_change", args=[self.payment.pk])
        delete = reverse("admin:core_ledgerentry_delete", args=[self.payment.pk])
        self.assertEqual(self.client.post(change, {"amount": "1.00"}).status_code, 403)
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, Decimal("500.00"))
        for name in (
            "admin:core_feeplan_changelist",
            "admin:core_ledgerentry_changelist",
            "admin:core_attendancesession_changelist",
            "admin:core_attendancechange_changelist",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class SeedDemoTests(TestCase):
    def seed(self):
        call_command("seed_demo", stdout=StringIO())

    @override_settings(DEBUG=False)
    def test_refuses_to_run_when_debug_is_off(self):
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_refuses_when_data_already_exists(self):
        Student.objects.create(full_name="Real Student", phone="9000000000")
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(Student.objects.count(), 1)
        self.assertFalse(User.objects.filter(username=OWNER_USERNAME).exists())

    @override_settings(DEBUG=True)
    def test_refuses_to_run_twice(self):
        self.seed()
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(Batch.objects.count(), 3)

    @override_settings(DEBUG=True)
    def test_creates_realistic_demo_data(self):
        self.seed()
        self.assertEqual(Batch.objects.count(), 3)
        self.assertEqual(Student.objects.count(), 15)
        self.assertEqual(Enrollment.objects.count(), 16)
        self.assertEqual(FeePlan.objects.count(), 16)
        self.assertEqual(LedgerEntry.objects.filter(kind="reversal").count(), 1)
        self.assertGreater(LedgerEntry.objects.filter(kind="payment").count(), 20)
        self.assertGreater(AttendanceSession.objects.count(), 10)
        self.assertEqual(User.objects.filter(role=User.Role.TEACHER).count(), 3)
        self.assertEqual(User.objects.filter(role=User.Role.STUDENT).count(), 5)

    @override_settings(DEBUG=True)
    def test_every_fee_plan_is_consistent_and_never_overpaid(self):
        self.seed()
        for plan in FeePlan.objects.prefetch_related("installments"):
            amounts = [item.amount for item in plan.installments.all()]
            self.assertEqual(sum(amounts), plan.total_amount)
            self.assertTrue(all(amount > 0 for amount in amounts))
            paid = net_paid(plan)
            self.assertGreaterEqual(paid, 0)
            self.assertLessEqual(paid, plan.total_amount)

    @override_settings(DEBUG=True)
    def test_demo_has_paid_up_overdue_and_unpaid_students(self):
        self.seed()
        rows = list(dues_report())
        self.assertTrue([r for r in rows if r.overdue > 0])
        self.assertTrue([r for r in rows if r.overdue == 0 and r.paid > 0])
        self.assertTrue([r for r in rows if r.paid == 0])
        # the payment entered for the wrong student was reversed
        vihaan = [r for r in rows if r.enrollment.student.full_name == "Vihaan Patil"]
        self.assertEqual(vihaan[0].paid, Decimal("0.00"))

    @override_settings(DEBUG=True)
    def test_demo_accounts_work_and_are_scoped(self):
        self.seed()
        owner = User.objects.get(username=OWNER_USERNAME)
        self.assertEqual(owner.role, User.Role.OWNER)
        self.assertTrue(owner.check_password(DEMO_PASSWORD))
        for username in TEACHER_USERNAMES:
            teacher = User.objects.get(username=username)
            self.assertTrue(teacher.check_password(DEMO_PASSWORD))
            self.assertEqual(batches_for(teacher).count(), 1)
            self.assertEqual(fee_plans_for(teacher).count(), 0)
        for username in PARENT_USERNAMES:
            parent = User.objects.get(username=username)
            self.assertTrue(parent.check_password(DEMO_PASSWORD))
            self.assertTrue(Student.objects.filter(user=parent).exists())
            plans = fee_plans_for(parent)
            self.assertTrue(plans.exists())
            self.assertFalse(plans.exclude(enrollment__student__user=parent).exists())
        kabir = Student.objects.get(full_name="Kabir Menon")
        self.assertEqual(kabir.enrollments.count(), 2)

    @override_settings(DEBUG=True)
    def test_dues_report_is_one_query_on_the_demo_data(self):
        self.seed()
        owner = User.objects.get(username=OWNER_USERNAME)
        with self.assertNumQueries(1):
            rows = list(dues_report(fee_plans=fee_plans_for(owner)))
            for row in rows:
                row.enrollment.student.full_name
                row.enrollment.batch.name
                row.paid, row.overdue, row.balance
        self.assertEqual(len(rows), 16)

    @override_settings(DEBUG=True)
    def test_existing_owner_is_used_and_no_demo_owner_is_created(self):
        existing = User.objects.create_superuser("real_owner", password="pw")
        self.seed()
        self.assertFalse(User.objects.filter(username=OWNER_USERNAME).exists())
        self.assertFalse(LedgerEntry.objects.exclude(received_by=existing).exists())

    @override_settings(DEBUG=True)
    def test_attendance_covers_enrolled_students_on_recent_non_sundays(self):
        self.seed()
        today = timezone.localdate()
        sessions = list(AttendanceSession.objects.select_related("batch"))
        self.assertTrue(sessions)
        for session in sessions:
            self.assertLessEqual(session.date, today)
            self.assertLessEqual((today - session.date).days, 7)
            self.assertNotEqual(session.date.weekday(), 6)
            enrolled = set(
                Enrollment.objects.filter(batch=session.batch, is_active=True)
                .values_list("student_id", flat=True)
            )
            marked = set(session.records.values_list("student_id", flat=True))
            self.assertEqual(marked, enrolled)
        self.assertTrue(AttendanceChange.objects.exists())