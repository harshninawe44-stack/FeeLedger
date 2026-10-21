# FeeLedger

A fee and attendance manager for coaching classes, with an append-only
payment ledger, batch-scoped teacher access, and dues reports computed in SQL.

## Problem

Small coaching classes track fees and attendance in notebooks and
spreadsheets. Numbers get overwritten, dues are hard to see, and teachers
can see more than they should.

## Roles

| Role | Can do |
|---|---|
| Owner | Manage batches, students and fee plans. Record and reverse payments. See all reports. Import CSV. |
| Teacher | Mark and edit attendance for their own batches only. |
| Student / Parent | Read-only view of their own fees, dues and attendance. |

## Key design decisions

- **Append-only ledger:** payments are never edited or deleted. A mistake
  is fixed by adding a reversal entry, so the full history is preserved.
- **Exact money:** all amounts use Decimal, never floats.
- **Row-level permissions:** teachers only ever receive data for their own
  batches, enforced in the backend queries.
- **Efficient queries:** dues are computed with database aggregates, not
  per-student loops.

## Tech stack

Python, Django, Django REST Framework, PostgreSQL (React frontend planned).

## Setup

1. Clone the repo and create a virtual environment: `python -m venv venv`
2. Activate it: `.\venv\Scripts\activate` (Windows)
3. Install packages: `pip install -r requirements.txt`
4. Create a PostgreSQL database named `feeledger`
5. Copy `.env.example` to `.env` and fill in your values
6. Run `python manage.py migrate`
7. Run `python manage.py runserver`

## Status

In progress. Done: project setup, custom User with roles, Batch/Student/Enrollment,
FeePlan/Installment/LedgerEntry models with database constraints.
Next: append-only trigger, reverse_payment(), tests.