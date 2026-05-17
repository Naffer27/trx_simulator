# simulator/urls.py
from django.urls import path
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
    path("api/metrics/",                  metrics_view,                 name="metrics"),
    path("api/broker/monitoring/",        broker_monitoring_view,       name="broker_monitoring"),
    path("api/broker/snapshots/",         snapshots_view,               name="broker_snapshots"),
]
