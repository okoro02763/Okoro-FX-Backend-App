from django.db import models

# Create your models here.
from django.utils import timezone

class CurrencyPair(models.Model):
    symbol = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    deriv_symbol = models.CharField(max_length=20, blank=True, null=True)  # Deriv's symbol format
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.symbol

class PriceData(models.Model):
    currency_pair = models.ForeignKey(CurrencyPair, on_delete=models.CASCADE)
    bid_price = models.DecimalField(max_digits=12, decimal_places=6)
    ask_price = models.DecimalField(max_digits=12, decimal_places=6)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['currency_pair', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.currency_pair.symbol} - {self.bid_price}/{self.ask_price}"

class Trade(models.Model):
    TRADE_TYPES = [
        ('BUY', 'Buy'),
        ('SELL', 'Sell'),
    ]
    
    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
        ('PENDING', 'Pending'),
    ]

    currency_pair = models.ForeignKey(CurrencyPair, on_delete=models.CASCADE)
    trade_type = models.CharField(max_length=4, choices=TRADE_TYPES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='OPEN')
    entry_price = models.DecimalField(max_digits=12, decimal_places=6)
    exit_price = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    profit_loss = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # Deriv-specific fields
    deriv_contract_id = models.CharField(max_length=50, blank=True, null=True)
    deriv_transaction_id = models.CharField(max_length=50, blank=True, null=True)
    
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-opened_at']
        indexes = [
            models.Index(fields=['status', '-opened_at']),
            models.Index(fields=['currency_pair', '-opened_at']),
        ]

    def __str__(self):
        return f"{self.trade_type} {self.currency_pair.symbol} - {self.status}"

class DerivAccount(models.Model):
    """Store Deriv account information"""
    account_id = models.CharField(max_length=50, unique=True)
    currency = models.CharField(max_length=10, default='USD')
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    is_demo = models.BooleanField(default=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Deriv Account: {self.account_id}"


class PasswordResetToken(models.Model):
    """One-time token used to reset a user's password."""
    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='password_reset_tokens',
    )
    key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)

    def __str__(self):
        return f"Reset token for {self.user.email}"


class BotState(models.Model):
    """Singleton-ish row storing whether the trading bot is running."""
    running = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Bot {'running' if self.running else 'stopped'}"


class StrategyConfig(models.Model):
    """Current strategy configuration shown in the strategy settings screen."""
    strategy = models.CharField(max_length=40, default='scalping')
    pairs = models.JSONField(default=list)
    risk_per_trade = models.DecimalField(max_digits=5, decimal_places=2, default=1.5)
    max_lots = models.DecimalField(max_digits=8, decimal_places=2, default=2.0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Strategy: {self.strategy}"


class EquityPoint(models.Model):
    """A sampled equity value over time for the dashboard equity curve."""
    timestamp = models.DateTimeField(default=timezone.now)
    value = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"{self.timestamp.isoformat()} -> {self.value}"


class Profile(models.Model):
    """One-to-one extension of the auth User with role and account status."""
    ROLES = [
        ('user', 'User'),
        ('admin', 'Admin'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('banned', 'Banned'),
    ]

    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='profile',
    )
    role = models.CharField(max_length=10, choices=ROLES, default='user')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    referral_code = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
    )
    must_change_password = models.BooleanField(default=False)
    account_type = models.CharField(
        max_length=10,
        choices=[('demo', 'Demo'), ('live', 'Live')],
        default='demo',
    )
    available_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=0
    )
    reward_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reffered_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invited_users',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} ({self.role}, {self.status})"


class Referral(models.Model):
    """Tracks a successful referral and the rewards issued to both parties."""
    referrer = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='referrals_made',
    )
    referee = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='referral',
    )
    code = models.CharField(max_length=20)
    referrer_reward = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    referee_reward = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.referrer.email} referred {self.referee.email}"


class AuditLog(models.Model):
    """Record of admin actions for the audit trail."""
    actor = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_actions',
    )
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=120, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.created_at.isoformat()} {self.action} -> {self.target}"


class FraudResult(models.Model):
    """Stored fraud-risk score and reasons for a user."""
    LEVELS = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='fraud',
    )
    score = models.IntegerField(default=0)
    level = models.CharField(max_length=20, choices=LEVELS, default='low')
    reasons = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} -> {self.level} ({self.score})"


class Withdrawal(models.Model):
    """A user's request to withdraw funds. A percentage service charge is
    applied; only the net amount minus fee is paid out."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('paid', 'Paid'),
    ]

    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='withdrawals',
    )
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    fee = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    bank_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=20)
    account_name = models.CharField(max_length=120)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending'
    )
    review_note = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='withdrawal_reviews',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} withdraw {self.amount} ({self.status})"


class IdentityVerification(models.Model):
    """A government-identity document submitted for KYC. Approving one grants
    access to the live (real-money) account."""
    DOC_TYPES = [
        ('nin', 'NIN'),
        ('national_id', 'National ID'),
        ('voters_card', "Voter's Card"),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='identity_verifications',
    )
    doc_type = models.CharField(max_length=20, choices=DOC_TYPES)
    doc_number = models.CharField(max_length=120)
    doc_image = models.ImageField(upload_to='kyc/%Y/%m/%d/')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending'
    )
    review_note = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_reviews',
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.user.email} - {self.get_doc_type_display()} ({self.status})"


class Invite(models.Model):
    """Outstanding admin invitation. The invitee must accept it by setting
    their own password, which is when their admin account is really created."""
    email = models.EmailField(unique=True, db_index=True)
    token = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='admin_invites_sent',
    )
    accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Invite for {self.email} ({'accepted' if self.accepted else 'pending'})"


class Payment(models.Model):
    """A funding payment initiated with Paystack.

    This row is the authoritative record of what a given reference is worth.
    The webhook credits `credit_amount` from here -- never from the inbound
    request body -- and the unique constraint on `reference` is what makes
    redelivery idempotent.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='payments',
    )
    reference = models.CharField(max_length=120, unique=True, db_index=True)
    # Total actually charged by Paystack, in naira.
    amount_charged = models.DecimalField(max_digits=15, decimal_places=2)
    # Service charge levied on top of the requested deposit.
    fee = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    # What actually lands in the wallet once the charge is taken.
    credit_amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=10, default='NGN')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending'
    )
    paystack_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.reference} ({self.user.email}, {self.status})"


class TrustedDevice(models.Model):
    user = models.ForeignKey(
        'auth.User', on_delete=models.CASCADE, related_name='trusted_devices'
    )
    device_id = models.CharField(max_length=64)
    label = models.CharField(max_length=255, blank=True, default='')
    ip_address = models.CharField(max_length=64, blank=True, default='')
    user_agent = models.TextField(blank=True, default='')
    last_ip = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    is_trusted = models.BooleanField(default=False)

    class Meta:
        unique_together = [('user', 'device_id')]
        ordering = ['-last_seen_at']

    def __str__(self):
        return f"{self.user.email} - {self.label or self.device_id}"


class LoginVerification(models.Model):
    user = models.ForeignKey(
        'auth.User', on_delete=models.CASCADE, related_name='login_verifications'
    )
    device_id = models.CharField(max_length=64)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class WebAuthnCredential(models.Model):
    user = models.ForeignKey(
        'auth.User', on_delete=models.CASCADE, related_name='webauthn_credentials'
    )
    credential_id = models.TextField()
    public_key = models.TextField()
    sign_count = models.IntegerField(default=0)
    device_label = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} - {self.device_label}"


class SecurityEvent(models.Model):
    user = models.ForeignKey(
        'auth.User', on_delete=models.CASCADE, related_name='security_events'
    )
    event_type = models.CharField(max_length=60)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
