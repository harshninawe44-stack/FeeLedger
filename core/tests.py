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