# simulator/urls.py
from django.urls import path
from .views import (
    login_view,
    logout_view,
    register_view,
    trading_dashboard,
    api_orden,
    clean_dashboard,
)

app_name = 'simulator'

urlpatterns = [
    # 1️⃣ Registro de usuarios
    path("register/", register_view, name="register"),

    # 2️⃣ Login con código
    path("login/", login_view, name="login"),

    # 3️⃣ Logout
    path("logout/", logout_view, name="logout"),

    # 4️⃣ Dashboard principal (raíz del sitio)
    path("", trading_dashboard, name="dashboard"),

    # 5️⃣ Ruta alternativa al dashboard
    path("dashboard/", trading_dashboard, name="dashboard_alt"),

    # 6️⃣ API de órdenes (AJAX desde el gráfico)
    path("api/orden/", api_orden, name="api_orden"),

 
]