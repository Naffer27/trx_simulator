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
    history_view,
    deposit_view,
    deposit_callback,
    deposit_history_view,
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

    # Dashboard (requiere login)
    path("dashboard/", trading_dashboard, name="dashboard"),
    path("dashboard/alt/", trading_dashboard, name="dashboard_alt"),

    # History
    path("history/", history_view, name="history"),

    # Depósitos
    path("deposit/", deposit_view, name="deposit"),
    path("deposit/callback/", deposit_callback, name="deposit_callback"),
    path("deposit/history/", deposit_history_view, name="deposit_history"),

    # API
    path("api/orden/", api_orden, name="api_orden"),
]
