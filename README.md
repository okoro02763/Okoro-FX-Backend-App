# forex-trader-backend

Django backend for **OKORO FX** — a Forex trading platform (trading, wallet,
Paystack funding/withdrawals, KYC, admin, referrals) with a reusable
transactional email system delivered through **Termii**.

---

## Email system

All email templates live in the codebase and are rendered with Django's
template engine. Delivery is done by whichever Django email backend the
environment selects (Termii when `TERMII_API_KEY` is set, otherwise SMTP or the
console backend).

### Layout

```
templates/emails/
  base.html                Shared OKORO FX layout (dark navy + teal, responsive,
                           inline CSS, table-based, no JS)
  welcome.html / .txt
  verify_email.html / .txt
  password_reset.html / .txt
  password_changed.html / .txt
  login_alert.html / .txt
  deposit_confirmation.html / .txt
  withdrawal_update.html / .txt
  trade_notification.html / .txt
  admin_invite.html / .txt
  termii_email_template.html   Source-of-truth mirror of the dashboard template

forex/email_service.py     Reusable service: render + send
forex/email_backend.py     TermiiEmailBackend (Django backend -> Termii HTTP API)
forex/management/commands/send_test_email.py   Safe preview / smoke test
forex/management/commands/termii_status.py     Read-only Termii history lookup
forex/tests_email.py       Email test suite
```

### How it works

`forex/email_service.py` keeps an `EMAILS` registry mapping a logical name to a
subject line plus the HTML and plain-text templates. Sending is a single call:

```python
from forex.email_service import send_template_email

send_template_email(
    'deposit_confirmation',      # logical template name
    user.email,                  # recipient
    {
        'name': user.first_name,
        'amount': '1000.00',
        'currency': 'NGN',
        'reference': reference,
        'status': 'success',
    },
    defer=True,                  # send only after the DB transaction commits
)
```

`build_template_message()` renders the HTML part, renders the `.txt` part as the
plain-text alternative, and returns a Django `EmailMultiAlternatives`.

Every send is failure-tolerant: `send_template_email` never raises, logs a
warning on failure, and returns `None`. Use `defer=True` for anything tied to a
database change (money, withdrawals, signup) so a rolled-back transaction or a
retried webhook never sends an email. Never log or email passwords, OTPs, reset
tokens, or API keys.

### Templates and dynamic variables

`base.html` provides the shared branding (brand name, tagline
*TRADE GLOBAL. TRADE SMARTER.*, CTA button, support block, footer) and blocks:
`title`, `preheader`, `heading`, `content`, `action`, `note`. Individual
templates extend it.

Common context variables (all optional — defaults are applied by
`default_context()`): `name`, `action_url`, `action_label`, `support_email`,
`brand_tagline`, `current_year`, plus per-email values such as `amount`,
`currency`, `reference`, `status`, `net_amount`, `symbol`, `trade_type`,
`quantity`, `entry_price`, `exit_price`, `profit_loss`, `status_note`,
`ip_address`, `user_agent`, `inviter`, `login_time`.

## How to add a new template

1. Create `templates/emails/my_email.html` extending `base.html` and set the
   `heading`/`content` blocks (and `preheader`/`note`/`action` as needed).
2. Create `templates/emails/my_email.txt` with the plain-text equivalent.
3. Register it in the `EMAILS` dict in `forex/email_service.py`:

   ```python
   'my_email': EmailSpec('Your subject', 'my_email.html', 'my_email.txt'),
   ```

4. Call `send_template_email('my_email', to_email, context, defer=True)` from the
   workflow — only for events the application actually implements.
5. Add a rendering/subject test in `forex/tests_email.py`.
6. If you use Termii, add the matching placeholder(s) to the dashboard template
   (see below) and, optionally, a per-type template id env var
   `TERMII_TEMPLATE_MY_EMAIL_ID`.

## Environment variables

See `backend/.env.example` for the full list. Email-relevant variables:

| Variable | Purpose |
| --- | --- |
| `TERMII_API_KEY` | When set, Termii becomes the email transport (takes priority over SMTP). |
| `TERMII_BASE_URL` | Termii API base URL. Default `https://v4.api.termii.com`. |
| `TERMII_EMAIL_CONFIG_ID` | Email configuration id from the Termii dashboard (defines the sender). |
| `TERMII_TEMPLATE_ID` | Default Termii dashboard template id. |
| `TERMII_BODY_VARIABLE` | Placeholder that receives the rendered body. Default `message`. |
| `TERMII_TIMEOUT` | HTTP timeout in seconds. Default `30`. |
| `EMAIL_GENERAL` | From address for the SMTP transport (note: **not** `DEFAULT_FROM_EMAIL`). |
| `DEFAULT_SUPPORT_EMAIL` | Support address rendered inside emails. |
| `FRONTEND_URL` | Base URL used to build verify/reset/invite links. |
| `EMAIL_HOST` / `EMAIL_USER` / `EMAIL_PASSWORD` | SMTP fallback transport. |

Never commit real credentials. Copy `.env.example` to `.env` (git-ignored).

Market-data variables (see `market/providers.py`):

| Variable | Purpose |
| --- | --- |
| `TWELVE_DATA_API_KEY` | Real forex/commodity/stock quotes + candles (free tier ≈ 8 credits/min). |
| `TWELVE_DATA_BASE_URL` | Twelve Data base URL. Default `https://api.twelvedata.com`. |
| `COINGECKO_API_KEY` | Real crypto quotes/candles (one batched request per tick). |
| `COINGECKO_BASE_URL` | CoinGecko base. Default `https://api.coingecko.com/api/v3` (Pro: `.../pro-api.coingecko.com/api/v3`). |
| `TWELVE_DATA_REQUESTS_PER_MINUTE` | Request guard; beyond it the last known good quote is served. Default `8`. |
| `COINGECKO_REQUESTS_PER_MINUTE` | Request guard for CoinGecko. Default `30`. |
| `MARKET_QUOTE_TTL` | Seconds a quote is cached. Default `3`. |

Without a key the market app serves a deterministic simulated feed, so the
backend runs offline.

## Termii configuration

Termii's email API is **template-based** — it does **not** accept arbitrary
HTML. Every send is rendered from a template created on the Termii dashboard,
and the verified endpoint is:

```
POST {TERMII_BASE_URL}/api/templates/send-email
{ api_key, email, subject, email_configuration_id, template_id, variables }
```

Because of this, `TermiiEmailBackend` maps a Django message onto that API:

- the codebase HTML/`.txt` is rendered normally and its **body is injected into
  `variables[TERMII_BODY_VARIABLE]`** (default `message`);
- only `name`, `action_url`, and `action_label` are forwarded as separate
  template variables (the button);
- everything else (amounts, references, trade details) stays inside the body.

**Required Termii dashboard setup** (one-time):

1. Create an **email configuration** on the Termii dashboard and copy its id to
   `TERMII_EMAIL_CONFIG_ID`.
2. Create a **template** whose body contains the `{{message}}` placeholder (or
   set `TERMII_BODY_VARIABLE` to the placeholder you used). It should also
   accept `{{name}}`, `{{action_url}}`, and `{{action_label}}`. Use
   `templates/emails/termii_email_template.html` as the source-of-truth mirror.
3. Copy the template id to `TERMII_TEMPLATE_ID` (or set a per-email
   `TERMII_TEMPLATE_<NAME>_ID`).

**Webhooks are not required.** Termii's documented webhook events cover outbound
SMS/WhatsApp delivery reports and WhatsApp device status only — there is **no
email delivery callback**. Since this project sends email only, no webhook URL
needs to be configured. Email delivery can be checked read-only with
`python manage.py termii_status <recipient-or-message-id>`.

## Running the email tests

No real email is sent: the transport is forced to Django's locmem backend and
all Termii HTTP calls are mocked.

```bash
python manage.py test forex.tests_email          # email suite only
python manage.py test                            # full suite
```

Covered: rendering every template, HTML + plain-text parts, subject/recipient,
missing/unknown template handling, Termii payload shape and error paths,
reset/verification link integrity (and no password leakage), workflow triggers,
and duplicate-notification prevention on webhook redelivery.

## Previewing templates safely (development)

Render locally without sending anything:

```bash
python manage.py shell -c "from django.template.loader import render_to_string; \
print(render_to_string('emails/welcome.html', {'name': 'Ada', \
'action_url': 'https://example.com', 'action_label': 'Continue', \
'support_email': 'support@okorofx.com', 'brand_tagline': 'Trade Global. Trade Smarter.', \
'current_year': 2026}))"
```

Or, with `DEBUG=True` and no Termii/SMTP transport configured, the console
backend prints the full message:

```bash
python manage.py send_test_email you@example.com --type welcome
```

`send_test_email` accepts `--type`, `--subject`, `--action-url`,
`--action-label`, and `--no-action`, and works against whichever transport is
configured (use a test address — it sends for real when Termii/SMTP is set).

## Notification map

| Workflow | Template | Trigger |
| --- | --- | --- |
| Registration | `welcome` | `auth/signup` (deferred) |
| Password reset request | `password_reset` | `auth/forgot-password` (deferred) |
| Password reset done / changed | `password_changed` | `auth/reset-password`, `auth/change-password` (deferred) |
| Sign-in | `login_alert` | `auth/login` |
| Admin invitation | `admin_invite` | `auth/invite` (deferred) |
| Deposit confirmed | `deposit_confirmation` | Paystack webhook, first success (deferred, idempotent) |
| Withdrawal update | `withdrawal_update` | Admin withdrawal review (deferred) |
| Trade placed / closed | `trade_notification` | `place-trade`, `close-trade` (deferred) |

`verify_email` is registered and ready but intentionally **not** wired: the app
has no email-verification mechanism yet. Do not invent a trigger for it.

## Deployment (Render)

The service is defined by `render.yaml`. Two things matter:

1. **Migrations run automatically.** `preDeployCommand: python manage.py migrate
   --noinput` runs before each release starts, so a new model can never be
   served against an old schema. (`railway.toml` and `docker-compose.yml` run
   the same command; the `Dockerfile` runs it in its `CMD`.) If you configure
   the service by hand instead of from `render.yaml`, set the **Pre-Deploy
   Command** to `python manage.py migrate --noinput` under *Settings → Build &
   Deploy*.
2. **Health checks catch a bad deploy.** `healthCheckPath: /api/health` — the
   endpoint returns `200` only when the database is reachable *and* there are no
   unapplied migrations, otherwise `503` with the pending migration names. A
   schema-behind release is then held back instead of routing users into 500s.

Set `DEBUG=False` (the default) in production. The trading views are also
hardened so the lazy settlement/trigger sweeps can never take down a read:
failures are logged via `logger.exception` and the caller still gets their
trades. All logs go to **stdout** (Render captures it) at `DJANGO_LOG_LEVEL`
(default `INFO`); 500s log a plain traceback, never the HTML debug page.

### Health endpoint

```bash
curl -s https://<service>/api/health
# {"status":"ok","database":"ok","migrations":"ok","unapplied_migrations":[]}
```

It is public and returns only booleans plus any pending migration names — never
configuration or secrets.
