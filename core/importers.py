"""CSV import of students and their batch enrollments."""

import csv
import io
import re
from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Batch, Enrollment, Student

MAX_BYTES = 1_000_000  # about 1 MB
MAX_ROWS = 1000
REQUIRED_COLUMNS = ["full_name", "phone", "batch"]
PHONE_PATTERN = re.compile(r"^\+?[0-9]{10,13}$")


class ImportFileError(Exception):
    """The file as a whole cannot be used (wrong format, too big, bad headers)."""


def _read_rows(raw):
    """Turn the uploaded bytes into a list of (line_number, row) pairs."""
    if len(raw) > MAX_BYTES:
        raise ImportFileError("The file is too large (the limit is 1 MB).")
    try:
        # utf-8-sig also removes the invisible marker Excel puts at the start.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError(
            "The file must be a UTF-8 CSV. In Excel, choose 'CSV UTF-8' when saving."
        ) from None

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportFileError("The file is empty.")
    reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    missing = [col for col in REQUIRED_COLUMNS if col not in reader.fieldnames]
    if missing:
        raise ImportFileError("Missing column(s): " + ", ".join(missing) + ".")

    rows = []
    for record in reader:
        row = {key: (value or "").strip() for key, value in record.items() if key}
        if not any(row.values()):
            continue  # a blank line
        if len(rows) >= MAX_ROWS:
            raise ImportFileError(f"Too many rows (the limit is {MAX_ROWS} per file).")
        rows.append((reader.line_num, row))
    if not rows:
        raise ImportFileError("The file has no data rows.")
    return rows


def _clean_phone(value):
    return re.sub(r"[\s\-()]", "", value)  # drop spaces, dashes and brackets


def _validate(rows):
    """Check every row. Returns (plan, errors). Nothing is saved here."""
    errors = []

    def fail(line, field, message):
        errors.append({"row": line, "field": field, "message": message})

    batches_by_name = {}
    for batch in Batch.objects.all():
        batches_by_name.setdefault(batch.name.strip().lower(), []).append(batch)

    # Pass 1: checks that need only the row itself.
    parsed = []
    for line, row in rows:
        before = len(errors)
        name = row.get("full_name", "")
        phone = _clean_phone(row.get("phone", ""))
        guardian = row.get("guardian_name", "")
        batch_name = row.get("batch", "")
        joined_text = row.get("joined_on", "")
        batch = None
        joined_on = timezone.localdate()

        if not name:
            fail(line, "full_name", "Name is required.")
        elif len(name) > 150:
            fail(line, "full_name", "Name is too long (150 characters at most).")

        if not phone:
            fail(line, "phone", "Phone is required.")
        elif not PHONE_PATTERN.match(phone):
            fail(line, "phone", "Phone must be 10 to 13 digits, optionally starting with +.")

        if len(guardian) > 150:
            fail(line, "guardian_name", "Guardian name is too long (150 characters at most).")

        if not batch_name:
            fail(line, "batch", "Batch is required.")
        else:
            matches = batches_by_name.get(batch_name.lower(), [])
            if not matches:
                fail(line, "batch", f"No batch is named '{batch_name}'.")
            elif len(matches) > 1:
                fail(line, "batch", f"More than one batch is named '{batch_name}'.")
            elif not matches[0].is_active:
                fail(line, "batch", f"Batch '{batch_name}' is not active.")
            else:
                batch = matches[0]

        if joined_text:
            try:
                joined_on = date.fromisoformat(joined_text)
            except ValueError:
                fail(line, "joined_on", "Date must look like 2026-06-01.")

        if len(errors) == before:
            parsed.append(
                {
                    "line": line,
                    "full_name": name,
                    "phone": phone,
                    "guardian_name": guardian,
                    "batch": batch,
                    "joined_on": joined_on,
                    "student": None,
                }
            )

    # Pass 2: checks against existing data, done in bulk (2 queries, not one per row).
    existing = {}
    phones = {item["phone"] for item in parsed}
    for student in Student.objects.filter(phone__in=phones):
        key = (student.full_name.strip().lower(), student.phone)
        existing.setdefault(key, []).append(student)

    unique_ids = [found[0].pk for found in existing.values() if len(found) == 1]
    enrolled = set(
        Enrollment.objects.filter(student_id__in=unique_ids).values_list(
            "student_id", "batch_id"
        )
    )

    first_seen = {}
    plan = []
    for item in parsed:
        key = (item["full_name"].lower(), item["phone"])
        found = existing.get(key, [])
        if len(found) > 1:
            fail(
                item["line"],
                "full_name",
                "More than one existing student has this name and phone. Fix this one by hand.",
            )
            continue
        student = found[0] if found else None

        pair = (key, item["batch"].pk)
        if pair in first_seen:
            fail(
                item["line"],
                "batch",
                f"The same student and batch already appear on line {first_seen[pair]}.",
            )
            continue
        first_seen[pair] = item["line"]

        if student is not None and (student.pk, item["batch"].pk) in enrolled:
            fail(item["line"], "batch", f"{student.full_name} is already enrolled in this batch.")
            continue

        item["student"] = student
        plan.append(item)

    errors.sort(key=lambda e: e["row"])
    return plan, errors


@transaction.atomic
def _save(plan):
    created_in_this_file = {}
    for item in plan:
        key = (item["full_name"].lower(), item["phone"])
        student = item["student"]
        if student is None:
            student = created_in_this_file.get(key)
        if student is None:
            student = Student.objects.create(
                full_name=item["full_name"],
                phone=item["phone"],
                guardian_name=item["guardian_name"],
            )
            created_in_this_file[key] = student
        Enrollment.objects.create(
            student=student, batch=item["batch"], joined_on=item["joined_on"]
        )


def import_students(*, raw, commit=False):
    """Validate a CSV upload and, if `commit` is true and it is clean, save it."""
    rows = _read_rows(raw)
    plan, errors = _validate(rows)
    if errors:
        return {"committed": False, "rows": len(rows), "errors": errors}

    summary = {
        "committed": False,
        "rows": len(rows),
        "new_students": len(
            {(p["full_name"].lower(), p["phone"]) for p in plan if p["student"] is None}
        ),
        "existing_students_matched": len(
            {p["student"].pk for p in plan if p["student"] is not None}
        ),
        "new_enrollments": len(plan),
        "errors": [],
    }
    if commit:
        try:
            _save(plan)
        except IntegrityError:
            raise ImportFileError(
                "The data changed while importing. Nothing was saved. "
                "Please upload the file again."
            ) from None
        summary["committed"] = True
    return summary