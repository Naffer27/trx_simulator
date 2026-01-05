# trx_simulator/urls.py
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Monta TODAS las rutas del app en la raíz "/" con namespace "simulator"
    path('', include(('simulator.urls', 'simulator'), namespace='simulator')),

    # Alias opcional: si alguien entra por /simulator/ lo mandamos a la raíz
    path('simulator/', RedirectView.as_view(url='/', permanent=False)),
]