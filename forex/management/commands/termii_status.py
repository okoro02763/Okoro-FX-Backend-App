import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Check Termii delivery status (History API) by recipient or message id.'

    def add_arguments(self, parser):
        parser.add_argument(
            'query',
            nargs='?',
            help='A recipient email address, or a Termii message_id, to filter by.',
        )
        parser.add_argument(
            '--message-id',
            dest='message_id',
            help='Exact Termii message_id to look up.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=20,
            help='Maximum number of rows to display (default: 20).',
        )

    def handle(self, *args, **options):
        api_key = getattr(settings, 'TERMII_API_KEY', '') or ''
        if not api_key:
            raise CommandError('TERMII_API_KEY is not set; nothing to query.')
        base_url = (
            getattr(settings, 'TERMII_BASE_URL', 'https://v4.api.termii.com')
            or 'https://v4.api.termii.com'
        ).rstrip('/')
        timeout = getattr(settings, 'TERMII_TIMEOUT', 30)

        query = (options['query'] or '').strip()
        is_email = '@' in query
        message_id = options['message_id'] or (query if query and not is_email else None)
        recipient = query if is_email else None

        params = {'api_key': api_key}
        if message_id:
            params['message_id'] = message_id

        try:
            response = requests.get(
                f'{base_url}/api/sms/inbox', params=params, timeout=timeout
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CommandError(f'Termii history request failed: {exc}')

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise CommandError(f'Unexpected Termii response: {data!r}')

        if recipient:
            data = [
                row for row in data
                if (row.get('receiver') or '').lower() == recipient.lower()
            ]

        if not data:
            self.stdout.write('No messages found.')
            return

        for row in data[: options['limit']]:
            self.stdout.write(
                '{created:19}  {channel:>5}  {status:>10}  {receiver:<32}  '
                'sender={sender}  {mid}'.format(
                    created=str(row.get('created_at') or '?'),
                    channel=str(row.get('channel') or '?'),
                    status=str(row.get('status') or '?'),
                    receiver=str(row.get('receiver') or '?'),
                    sender=row.get('sender'),
                    mid=row.get('message_id') or '?',
                )
            )
        self.stdout.write(self.style.SUCCESS(f'{min(len(data), options["limit"])} message(s) shown.'))
