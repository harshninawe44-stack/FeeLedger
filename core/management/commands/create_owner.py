"""Create the first owner account from environment variables.

    OWNER_USERNAME=... OWNER_PASSWORD=... python manage.py create_owner

Safe to run on every start of the server: it does nothing once an owner exists,
and it never changes an existing account.
"""

import os

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Create the first owner account from OWNER_USERNAME and OWNER_PASSWORD."

    def handle(self, *args, **options):
        if User.objects.filter(role=User.Role.OWNER).exists():
            self.stdout.write("An owner account already exists. Nothing to do.")
            return

        username = os.getenv("OWNER_USERNAME", "").strip()
        password = os.getenv("OWNER_PASSWORD", "")
        if not username or not password:
            self.stdout.write(
                "No owner exists yet, and OWNER_USERNAME and OWNER_PASSWORD are "
                "not both set. Nothing was created."
            )
            return

        try:
            User._meta.get_field("username").run_validators(username)
        except ValidationError as error:
            raise CommandError("OWNER_USERNAME is not valid: " + " ".join(error.messages))
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError(f"The username '{username}' is already taken.")
        try:
            # Django's own password rules (length, common passwords, similarity).
            password_validation.validate_password(password, User(username=username))
        except ValidationError as error:
            raise CommandError("OWNER_PASSWORD is not strong enough: " + " ".join(error.messages))

        User.objects.create_superuser(username, password=password)
        self.stdout.write(self.style.SUCCESS(f"Owner account '{username}' created."))