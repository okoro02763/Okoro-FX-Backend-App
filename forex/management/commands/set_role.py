from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from forex.models import Profile

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Set a user's role by email. "
        "Example: python manage.py set_role you@example.com user"
    )

    def add_arguments(self, parser):
        parser.add_argument('email', help='Email of the account to update.')
        parser.add_argument('role', choices=['user', 'admin'], help='New role.')

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        role = options['role']

        user = User.objects.filter(email=email).first()
        if user is None:
            raise CommandError(f'No account found with email {email!r}.')

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.role = role
        profile.save(update_fields=['role'])

        self.stdout.write(self.style.SUCCESS(f'{email} is now role={role}.'))
