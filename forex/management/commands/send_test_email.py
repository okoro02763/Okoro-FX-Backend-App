from django.core.management.base import BaseCommand, CommandError

from forex.email_service import send_template_email


class Command(BaseCommand):
    help = 'Send a test transactional email through the configured transport.'

    def add_arguments(self, parser):
        parser.add_argument('email', help='Recipient email address.')
        parser.add_argument(
            '--type',
            default='welcome',
            help='Template name: welcome, password_reset, deposit_confirmation, ...',
        )
        parser.add_argument(
            '--subject',
            default=None,
            help='Override the default subject line.',
        )
        parser.add_argument(
            '--action-url',
            default='https://okorofx.netlify.app',
            help='Call-to-action URL (exposed as {{action_url}}).',
        )
        parser.add_argument(
            '--action-label',
            default='Open OKORO FX',
            help='Call-to-action label (exposed as {{action_label}}).',
        )
        parser.add_argument(
            '--no-action',
            action='store_true',
            help='Send without a call-to-action button/link.',
        )

    def handle(self, *args, **options):
        recipient = options['email'].strip().lower()
        if '@' not in recipient:
            raise CommandError(f"'{recipient}' does not look like an email address.")

        action_url = None if options['no_action'] else options['action_url']
        action_label = None if options['no_action'] else options['action_label']

        template_name = options['type']
        context = {
            'name': 'there',
            'action_url': action_url,
            'action_label': action_label,
            'inviter': 'Admin',
            'amount': '1000.00',
            'currency': 'NGN',
            'reference': 'TEST-REF-0001',
            'status': 'success',
            'net_amount': '985.00',
            'symbol': 'EUR/USD',
            'trade_type': 'BUY',
            'quantity': '1.00',
            'entry_price': '1.08050',
            'exit_price': '1.08100',
            'profit_loss': '0.50',
            'status_note': 'Reviewed and approved.',
            'ip_address': '127.0.0.1',
            'user_agent': 'Django test command',
        }

        self.stdout.write(f'Sending {template_name} email to {recipient} ...')
        msg = send_template_email(
            template_name,
            recipient,
            context,
            subject=options['subject'],
        )
        if msg is None:
            self.stdout.write(
                self.style.SUCCESS(
                    'Done. Check logs/output for transport result (no email sent or'
                    ' transport failed silently).'
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS('Done. Message sent via configured backend.'))
