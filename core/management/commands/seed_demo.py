"""Fill an EMPTY development database with realistic demo data.

    python manage.py seed_demo

Everything goes through the same service functions the app uses, so the demo
data obeys every ledger and attendance rule.
"""

import calendar
import random
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import AttendanceRecord, Batch, FeePlan, Student, User
from core.services import (
    create_fee_plan,
    create_student,
    enroll_student,
    mark_attendance,
    record_payment,
    reverse_payment,
)

DEMO_PASSWORD = "Demo-Pass-2026"
OWNER_USERNAME = "demo_owner"
TEACHERS = [
    ("demo_teacher1", "Meena Kulkarni"),
    ("demo_teacher2", "Rahul Deshpande"),
    ("demo_teacher3", "Sneha Joshi"),
]
PARENTS = [
    ("demo_parent1", "Sharma family"),
    ("demo_parent2", "Patil family"),
    ("demo_parent3", "Iyer family"),
    ("demo_parent4", "Menon family"),
    ("demo_parent5", "Gupta family"),
]
TEACHER_USERNAMES = [username for username, _ in TEACHERS]
PARENT_USERNAMES = [username for username, _ in PARENTS]

# name, teacher number, total fee, installments, days ago the first one fell due
BATCHES = [
    ("JEE Morning", 0, "60000.00", 4, 70),
    ("NEET Evening", 1, "48000.00", 4, 70),
    ("Maths Foundation", 2, "24000.00", 3, 40),
]

# name, guardian, parent number (or None), batches, payment pattern
STUDENTS = [
    ("Aarav Sharma", "Mr Sharma", 0, ["JEE Morning"], "current"),
    ("Diya Sharma", "Mr Sharma", 0, ["NEET Evening"], "behind"),
    ("Vihaan Patil", "Mrs Patil", 1, ["JEE Morning"], "none"),
    ("Ananya Iyer", "Mr Iyer", 2, ["NEET Evening"], "advance"),
    ("Kabir Menon", "Mrs Menon", 3, ["JEE Morning", "Maths Foundation"], "behind"),
    ("Ishita Bose", "Mr Bose", None, ["JEE Morning"], "partial"),
    ("Rohan Gupta", "Mrs Gupta", 4, ["NEET Evening"], "current"),
    ("Myra Khan", "Mr Khan", None, ["NEET Evening"], "none"),
    ("Arjun Reddy", "Mr Reddy", None, ["JEE Morning"], "current"),
    ("Zoya Ansari", "Mrs Ansari", None, ["JEE Morning"], "advance"),
    ("Neha Kapoor", "Mr Kapoor", None, ["NEET Evening"], "partial"),
    ("Saanvi Nair", "Mr Nair", None, ["Maths Foundation"], "current"),
    ("Dev Malhotra", "Mrs Malhotra", None, ["Maths Foundation"], "behind"),
    ("Tara Pillai", "Mr Pillai", None, ["Maths Foundation"], "none"),
    ("Yash Chauhan", "Mr Chauhan", None, ["Maths Foundation"], "current"),
]

METHODS = ["upi", "cash", "cash", "cheque", "bank"]


def add_months(day, months):
    """2026-01-31 + 1 month -> 2026-02-28 (the day is clamped to the month's end)."""
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def planned_payments(pattern, installments, today):
    """The (amount, date) payments a student with this pattern has made."""
    due = [item for item in installments if item.due_date <= today]
    if pattern == "current":
        chosen = due
    elif pattern == "behind":
        chosen = due[:-1]
    elif pattern == "advance":
        chosen = installments[: len(due) + 1]
    elif pattern == "partial":
        half = (installments[0].amount / 2).quantize(Decimal("0.01"))
        return [(half, min(installments[0].due_date, today))]
    else:
        chosen = []
    return [(item.amount, min(item.due_date, today)) for item in chosen]


class Command(BaseCommand):
    help = "Fill an empty development database with realistic demo data."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run because DEBUG is off. The demo creates "
                "accounts with a publicly known password."
            )
        if Batch.objects.exists() or Student.objects.exists():
            raise CommandError(
                "This database already has batches or students. Run the demo "
                "on a fresh, empty database."
            )
        wanted = [OWNER_USERNAME, *TEACHER_USERNAMES, *PARENT_USERNAMES]
        if User.objects.filter(username__in=wanted).exists():
            raise CommandError("A demo username already exists in this database.")

        rng = random.Random(2026)  # the same data every time
        today = timezone.localdate()

        owner = User.objects.filter(role=User.Role.OWNER).order_by("id").first()
        made_owner = owner is None
        if made_owner:
            owner = User.objects.create_superuser(OWNER_USERNAME, password=DEMO_PASSWORD)

        teachers = [
            User.objects.create_user(
                username, password=DEMO_PASSWORD, role=User.Role.TEACHER, first_name=name
            )
            for username, name in TEACHERS
        ]
        parents = [
            User.objects.create_user(
                username, password=DEMO_PASSWORD, role=User.Role.STUDENT, first_name=name
            )
            for username, name in PARENTS
        ]

        batches = {}
        for name, teacher_number, _total, _count, days_ago in BATCHES:
            batches[name] = Batch.objects.create(
                name=name,
                teacher=teachers[teacher_number],
                start_date=today - timedelta(days=days_ago + 30),
            )

        # Students and enrolments
        patterns = {}
        for number, (name, guardian, parent_number, batch_names, pattern) in enumerate(
            STUDENTS, start=1
        ):
            first = batches[batch_names[0]]
            student = create_student(
                full_name=name,
                phone=f"98000{number:05d}",
                guardian_name=guardian,
                user=parents[parent_number] if parent_number is not None else None,
                batch=first,
                joined_on=first.start_date,
            )
            for extra in batch_names[1:]:
                enroll_student(
                    student=student, batch=batches[extra], joined_on=batches[extra].start_date
                )
            patterns[student.pk] = pattern

        # Fee plans and payments
        plan_settings = {name: (total, count, days) for name, _t, total, count, days in BATCHES}
        payment_number = 0
        wrong_payment_plan = None
        for student in Student.objects.order_by("id"):
            for enrollment in student.enrollments.select_related("batch").order_by("id"):
                total, count, days_ago = plan_settings[enrollment.batch.name]
                first_due = today - timedelta(days=days_ago)
                plan = create_fee_plan(
                    enrollment=enrollment,
                    total_amount=total,
                    due_dates=[add_months(first_due, i) for i in range(count)],
                )
                installments = list(plan.installments.order_by("number"))
                pattern = patterns[student.pk]
                for amount, paid_on in planned_payments(pattern, installments, today):
                    method = METHODS[payment_number % len(METHODS)]
                    payment_number += 1
                    reference = {
                        "upi": f"UPI{rng.randint(100000, 999999)}",
                        "cheque": f"CHQ{rng.randint(100000, 999999)}",
                    }.get(method, "")
                    record_payment(
                        fee_plan=plan,
                        amount=amount,
                        method=method,
                        paid_on=paid_on,
                        received_by=owner,
                        reference=reference,
                    )
                if pattern == "none" and wrong_payment_plan is None:
                    wrong_payment_plan = plan

        # One payment entered against the wrong student, then reversed
        mistake = record_payment(
            fee_plan=wrong_payment_plan,
            amount=Decimal("5000.00"),
            method="cash",
            paid_on=today - timedelta(days=3),
            received_by=owner,
            note="Entered by mistake",
        )
        reverse_payment(
            entry=mistake,
            reason="Entered against the wrong student",
            received_by=owner,
        )

        # A week of attendance, marked by each batch's own teacher
        statuses = [choice for choice, _ in AttendanceRecord.Status.choices]
        for batch in batches.values():
            student_ids = list(
                batch.enrollments.filter(is_active=True).values_list("student_id", flat=True)
            )
            for days_back in range(7, 0, -1):
                day = today - timedelta(days=days_back)
                if day.weekday() == 6:  # no classes on Sunday
                    continue
                sheet = {
                    student_id: rng.choices(statuses, weights=[80, 10, 7, 3])[0]
                    for student_id in student_ids
                }
                mark_attendance(
                    batch=batch, date=day, statuses=sheet, marked_by=batch.teacher
                )

        self.stdout.write(self.style.SUCCESS("Demo data created."))
        self.stdout.write(f"Password for every demo account: {DEMO_PASSWORD}")
        if made_owner:
            self.stdout.write(f"  owner:    {OWNER_USERNAME}")
        else:
            self.stdout.write("  owner:    your existing owner account (unchanged)")
        self.stdout.write("  teachers: " + ", ".join(TEACHER_USERNAMES))
        self.stdout.write("  parents:  " + ", ".join(PARENT_USERNAMES))