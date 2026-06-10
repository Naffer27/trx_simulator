# simulator/urls.py
from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy
from .views import (
    login_view,
    logout_view,
    register_view,
    trading_dashboard,
    api_orden,
    clean_dashboard,
    landing_view,
    home_view,
    switch_account_view,
    history_view,
    deposit_view,
    deposit_callback,
    deposit_status_view,
    deposit_status_json,
    deposit_history_view,
    wallet_balance_json,
    np_diagnostics_view,
    np_supported_currencies_view,
    health_check,
    health_detail_view,
    metrics_view,
    broker_monitoring_view,
    snapshots_view,
    # Withdrawals
    withdraw_view,
    withdraw_history_view,
    withdraw_payout_callback,
    # Capital flow
    accounts_view,
    create_account_view,
    fund_account_view,
    withdraw_account_view,
    # Staff operational panel
    ops_panel_view,
    # 2FA
    totp_setup_view,
    totp_verify_view,
    totp_disable_view,
    # Email verification
    verify_email_view,
    resend_verification_view,
    # Legal acceptance
    accept_terms_view,
    # Broker ecosystem modules
    calendar_view,
    associates_view,
    referral_click_view,
    bonuses_view,
    documents_view,
    experts_view,
    # Challenge purchase
    challenge_catalog_view,
    challenge_purchase_view,
    # External webhook + status API
    external_challenge_activate,
    challenge_status_view,
    # Account catalog
    account_open_view,
    # KYC
    kyc_view,
    # Profile
    profile_view,
)

app_name = 'simulator'

urlpatterns = [
    # Landing pública
    path("", landing_view, name="landing"),

    # Autenticación
    path("register/", register_view, name="register"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),

    # Home dashboard (after login)
    path("home/", home_view, name="home"),

    # ── My Accounts (capital flow) ──────────────────────────────
    path("accounts/",                               accounts_view,          name="accounts"),
    path("accounts/open/",                          account_open_view,      name="account_open"),
    path("accounts/create/",                        create_account_view,    name="create_account"),
    path("accounts/<int:account_id>/fund/",         fund_account_view,      name="fund_account"),
    path("accounts/<int:account_id>/withdraw/",     withdraw_account_view,  name="withdraw_account"),

    # ── Dashboard ───────────────────────────────────────────────
    path("dashboard/<int:account_id>/", trading_dashboard, name="dashboard_account"),
    path("dashboard/",                  trading_dashboard, name="dashboard"),
    path("dashboard/alt/",              trading_dashboard, name="dashboard_alt"),

    # Account switcher (legacy — session-based)
    path("accounts/switch/<int:account_id>/", switch_account_view, name="switch_account"),

    # History
    path("history/", history_view, name="history"),

    # Retiros crypto
    path("withdraw/",          withdraw_view,             name="withdraw"),
    path("withdraw/callback/", withdraw_payout_callback,  name="withdraw_payout_callback"),
    path("withdraw/history/",  withdraw_history_view,     name="withdraw_history"),

    # Depósitos
    path("deposit/",                              deposit_view,         name="deposit"),
    path("deposit/callback/",                     deposit_callback,     name="deposit_callback"),
    path("deposit/<int:deposit_id>/",             deposit_status_view,  name="deposit_status"),
    path("deposit/<int:deposit_id>/status.json",  deposit_status_json,  name="deposit_status_json"),
    path("deposit/history/",                      deposit_history_view, name="deposit_history"),

    # API
    path("api/orden/",                    api_orden,                    name="api_orden"),
    path("api/wallet-balance/",           wallet_balance_json,          name="wallet_balance"),
    path("api/np-check/",                 np_diagnostics_view,          name="np_check"),
    path("api/np-supported-currencies/",  np_supported_currencies_view, name="np_supported_currencies"),
    path("api/health/",                   health_check,                 name="health_check"),
    path("api/health/detail/",            health_detail_view,           name="health_detail"),
    path("api/metrics/",                  metrics_view,                 name="metrics"),
    path("api/broker/monitoring/",        broker_monitoring_view,       name="broker_monitoring"),
    path("api/broker/snapshots/",         snapshots_view,               name="broker_snapshots"),

    # ── Staff Operational Panel ──────────────────────────────────────────────
    path("staff/ops/", ops_panel_view, name="ops_panel"),

    # ── 2FA ─────────────────────────────────────────────────────────────────
    path("account/2fa/setup/",   totp_setup_view,   name="totp_setup"),
    path("account/2fa/verify/",  totp_verify_view,  name="totp_verify"),
    path("account/2fa/disable/", totp_disable_view, name="totp_disable"),

    # ── Email verification ───────────────────────────────────────────────────
    path("verify-email/<str:token>/", verify_email_view,       name="verify_email"),
    path("resend-verification/",      resend_verification_view, name="resend_verification"),

    # ── Legal acceptance ─────────────────────────────────────────────────────
    path("legal/accept/", accept_terms_view, name="accept_terms"),

    # ── Broker Ecosystem Modules ─────────────────────────────────────────────
    path("calendar/",             calendar_view,       name="calendar"),
    path("associates/",           associates_view,     name="associates"),
    path("ref/<str:code>/",       referral_click_view, name="referral_click"),
    path("bonuses/",              bonuses_view,        name="bonuses"),
    path("documents/",            documents_view,      name="documents"),
    path("experts/",              experts_view,        name="experts"),
    path("kyc/",                  kyc_view,            name="kyc"),
    path("profile/",              profile_view,        name="profile"),

    # ── Challenge Purchase ────────────────────────────────────────────────────
    path("challenges/",                       challenge_catalog_view,  name="challenge_catalog"),
    path("challenges/<int:product_id>/buy/",  challenge_purchase_view, name="challenge_purchase"),

    # ── External Challenge Activation Webhook ────────────────────────────────
    path("api/internal/challenge/activate/",  external_challenge_activate, name="external_challenge_activate"),

    # ── Internal Challenge Status API (Phase 5G) ──────────────────────────────
    path("api/internal/challenge/status/<str:external_event_id>/", challenge_status_view, name="challenge_status"),

    # ── Password Change (authenticated users) ───────────────────────────────
    path("password-change/", auth_views.PasswordChangeView.as_view(
        template_name="simulator/password_change_form.html",
        success_url=reverse_lazy("simulator:password_change_done"),
    ), name="password_change"),
    path("password-change/done/", auth_views.PasswordChangeDoneView.as_view(
        template_name="simulator/password_change_done.html",
    ), name="password_change_done"),

    # ── Password Reset ───────────────────────────────────────────────────────
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="simulator/password_reset_form.html",
        email_template_name="simulator/password_reset_email.html",
        subject_template_name="simulator/password_reset_subject.txt",
        success_url=reverse_lazy("simulator:password_reset_done"),
    ), name="password_reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(
        template_name="simulator/password_reset_done.html",
    ), name="password_reset_done"),
    path("password-reset/confirm/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="simulator/password_reset_confirm.html",
        success_url=reverse_lazy("simulator:password_reset_complete"),
    ), name="password_reset_confirm"),
    path("password-reset/complete/", auth_views.PasswordResetCompleteView.as_view(
        template_name="simulator/password_reset_complete.html",
    ), name="password_reset_complete"),
]
