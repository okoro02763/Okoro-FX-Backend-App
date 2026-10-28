from django.urls import re_path
from . import views
from . import auth_views, bot_views, strategy_views, admin_views, analytics_views, referral_views, account_views, passkey_views


def api_route(route, view, name):
    """Register an API route that answers with or without a trailing slash.

    The frontend calls the API both ways (`/auth/signup` and `/account/`).
    With APPEND_SLASH the slash-less form used to earn a 301, and a redirect
    on a CORS preflight is rejected by the browser, so the trailing slash is
    made optional here instead of relying on the redirect.
    """
    return re_path(rf'^{route}/?$', view, name=name)


urlpatterns = [
    api_route('currency-pairs', views.get_currency_pairs, name='currency-pairs'),
    api_route('live-prices', views.get_live_prices, name='live-prices'),
    api_route(
        r'price-history/(?P<symbol>[^/]+)',
        views.get_price_history,
        name='price-history',
    ),
    api_route(
        r'technical-analysis/(?P<symbol>[^/]+)',
        views.get_technical_analysis,
        name='technical-analysis',
    ),
    api_route('place-trade', views.place_trade, name='place-trade'),
    api_route(
        r'close-trade/(?P<trade_id>[0-9]+)',
        views.close_trade,
        name='close-trade',
    ),
    api_route('trades', views.get_trades, name='trades'),
    api_route('portfolio', views.get_portfolio_summary, name='portfolio'),
    api_route('deriv-status', views.get_deriv_status, name='deriv-status'),
    api_route('account-info', views.get_account_info, name='account-info'),

    # Auth
    api_route('auth/login', auth_views.login, name='auth-login'),
    api_route('auth/verify-device', auth_views.verify_device, name='auth-verify-device'),
    api_route('auth/resend-verification', auth_views.resend_verification, name='auth-resend-verification'),
    api_route('auth/devices', auth_views.list_devices, name='auth-devices'),
    api_route(r'auth/devices/(?P<device_id>[0-9]+)', auth_views.revoke_device, name='auth-revoke-device'),
    api_route('auth/signup', auth_views.signup, name='auth-signup'),
    api_route('auth/validate-referral', auth_views.validate_referral, name='auth-validate-referral'),
    api_route('auth/forgot-password', auth_views.forgot_password, name='auth-forgot-password'),
    api_route('auth/reset-password', auth_views.reset_password, name='auth-reset-password'),
    api_route('auth/me', auth_views.me, name='auth-me'),
    api_route('auth/invite', auth_views.invite_user, name='auth-invite'),
    api_route('auth/accept-invite', auth_views.accept_invite, name='auth-accept-invite'),
    api_route('auth/change-password', auth_views.change_password, name='auth-change-password'),
    # Passkeys / Biometrics
    api_route('auth/passkey/register/begin', passkey_views.passkey_register_begin, name='auth-passkey-register-begin'),
    api_route('auth/passkey/register/finish', passkey_views.passkey_register_finish, name='auth-passkey-register-finish'),
    api_route('auth/passkey/login/begin', passkey_views.passkey_login_begin, name='auth-passkey-login-begin'),
    api_route('auth/passkey/login/finish', passkey_views.passkey_login_finish, name='auth-passkey-login-finish'),

    # Bot
    api_route('bot/status', bot_views.get_bot_status, name='bot-status'),
    api_route('bot/start', bot_views.start_bot, name='bot-start'),
    api_route('bot/stop', bot_views.stop_bot, name='bot-stop'),
    api_route('bot/equity', bot_views.get_equity, name='bot-equity'),
    api_route('bot/positions', bot_views.get_positions, name='bot-positions'),
    api_route('bot/trades', bot_views.get_bot_trades, name='bot-trades'),

    # Strategy
    api_route('strategy', strategy_views.strategy, name='strategy'),

    # Admin
    api_route('admin/users', admin_views.list_users, name='admin-users'),
    api_route(
        r'admin/users/(?P<user_id>[0-9]+)/status',
        admin_views.change_user_status,
        name='admin-user-status',
    ),
    api_route(
        r'admin/users/(?P<user_id>[0-9]+)/role',
        admin_views.change_user_role,
        name='admin-user-role',
    ),
    api_route(
        r'admin/users/(?P<user_id>[0-9]+)/fraud',
        admin_views.recompute_fraud,
        name='admin-user-fraud',
    ),
    api_route('admin/audit-logs', admin_views.list_audit_logs, name='admin-audit-logs'),

    # Fraud
    api_route('fraud/users', admin_views.fraud_scores, name='fraud-users'),

    # Analytics
    api_route('analytics/summary', analytics_views.analytics_summary, name='analytics-summary'),
    api_route('analytics/trades', analytics_views.analytics_trades, name='analytics-trades'),

    # Referrals
    api_route('referrals', referral_views.get_referrals, name='referrals'),

    # Account / KYC / Live funding
    api_route('account', account_views.account_status, name='account-status'),
    api_route('account/type', account_views.set_account_type, name='account-type'),
    api_route('account/kyc', account_views.submit_kyc, name='account-kyc'),
    api_route('account/live/fund', account_views.live_fund, name='account-live-fund'),
    api_route('account/withdrawals', account_views.list_withdrawals, name='account-withdrawals'),
    api_route('account/withdraw', account_views.create_withdrawal, name='account-withdraw'),
    api_route('paystack/webhook', account_views.paystack_webhook, name='paystack-webhook'),

    # Admin KYC
    api_route('admin/kyc', admin_views.list_kyc, name='admin-kyc'),
    api_route(
        r'admin/kyc/(?P<verification_id>[0-9]+)/review',
        admin_views.review_kyc,
        name='admin-kyc-review',
    ),

    # Admin Withdrawals
    api_route('admin/withdrawals', admin_views.list_withdrawals_admin, name='admin-withdrawals'),
    api_route(
        r'admin/withdrawals/(?P<withdrawal_id>[0-9]+)/review',
        admin_views.review_withdrawal,
        name='admin-withdrawal-review',
    ),
]