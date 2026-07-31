# simulator/tests/test_o2d_dashboard_navigation.py
"""
Bloque O.2d — Navegación visible de dashboards operativos.

Cubre únicamente lo añadido/verificado en este bloque:
  - BrokerLedgerAdmin.revenue_dashboard_link (list_display, nuevo)
  - BrokerRevenueSnapshotAdmin.analytics_link / control_center_link (list_display, nuevos)
  - Botón de cabecera "Broker Control Center" / "Broker Analytics" /
    "Revenue Dashboard →" (change_list.html model-specific, preexistente —
    verificado aquí que sobrevive con tabla vacía, no que se creó)
  - Enlace condicional a Shadow Exposure dentro de Broker Control Center
    (nuevo, gateado por is_superuser)
  - No regresión: las 8 URLs admin:* existentes siguen resolviendo,
    el endpoint JSON del Control Center sigue funcionando, Live Analytics
    y el Dealing Desk por cuenta siguen accesibles, y el guard is_superuser
    de shadow_exposure_view no fue tocado.

No toca modelos, migraciones, permisos base ni ninguna vista de negocio.
"""
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from simulator.models import BrokerLedger, BrokerRevenueSnapshot

from .factories import make_account, make_user


class HeaderButtonEmptyTableTests(TestCase):
    """Parte 1 — el botón de cabecera debe existir aunque la tabla esté vacía."""

    def setUp(self):
        self.assertEqual(BrokerRevenueSnapshot.objects.count(), 0)
        self.assertEqual(BrokerLedger.objects.count(), 0)
        superuser = make_user(username="o2d_super_empty", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(superuser)

    def test_control_center_button_visible_with_empty_brokerrevenuesnapshot(self):
        url = reverse("admin:simulator_brokerrevenuesnapshot_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Control Center")

    def test_broker_analytics_header_button_visible_with_empty_table(self):
        url = reverse("admin:simulator_brokerrevenuesnapshot_changelist")
        resp = self.client.get(url)
        self.assertContains(resp, "Broker Analytics")

    def test_revenue_dashboard_header_button_visible_with_empty_brokerledger(self):
        url = reverse("admin:simulator_brokerledger_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Revenue Dashboard")

    def test_control_center_url_itself_reachable_with_empty_table(self):
        resp = self.client.get(reverse("admin:brokerrevsnap_control"))
        self.assertEqual(resp.status_code, 200)


class ExistingURLResolutionTests(TestCase):
    """Ninguno de los 8 nombres de URL admin:* existentes cambió."""

    def test_all_existing_dashboard_urls_still_resolve(self):
        names = [
            "simulator_tradingaccount_dealing_desk",
            "broker_live_analytics",
            "broker_take_snapshot",
            "broker_shadow_exposure",
            "brokerledger_revenue_dashboard",
            "brokerrevsnap_analytics",
            "brokerrevsnap_control",
            "brokerrevsnap_control_data",
        ]
        for name in names:
            with self.subTest(name=name):
                if name == "simulator_tradingaccount_dealing_desk":
                    reverse(f"admin:{name}", args=[1])
                else:
                    reverse(f"admin:{name}")


class ListDisplayLinkTests(TestCase):
    """Parte 2 — enlaces directos por fila, cuando sí existen filas."""

    def setUp(self):
        superuser = make_user(username="o2d_super_rows", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(superuser)
        BrokerRevenueSnapshot.objects.create()
        BrokerLedger.objects.create(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("10.00"))

    def test_brokerrevenuesnapshot_row_shows_analytics_and_control_center_links(self):
        resp = self.client.get(reverse("admin:simulator_brokerrevenuesnapshot_changelist"))
        self.assertContains(resp, "→ Broker Analytics")
        self.assertContains(resp, "→ Broker Control Center")

    def test_brokerledger_row_shows_revenue_dashboard_link(self):
        resp = self.client.get(reverse("admin:simulator_brokerledger_changelist"))
        self.assertContains(resp, "→ Revenue Dashboard")


class ShadowExposureControlCenterVisibilityTests(TestCase):
    """Parte 3 — el enlace a Shadow Exposure solo se renderiza para superusuario."""

    def test_superuser_sees_shadow_exposure_link_in_control_center(self):
        superuser = make_user(username="o2d_super_cc", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(reverse("admin:brokerrevsnap_control"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Shadow Exposure")
        self.assertContains(resp, reverse("admin:broker_shadow_exposure"))

    def test_staff_non_superuser_does_not_see_shadow_exposure_link(self):
        staff = make_user(username="o2d_staff_cc", is_staff=True, is_superuser=False)
        client = Client()
        client.force_login(staff)

        resp = client.get(reverse("admin:brokerrevsnap_control"))

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Shadow Exposure")


class ShadowExposurePermissionUnchangedTests(TestCase):
    """No se amplió ni tocó el guard is_superuser de la vista en sí."""

    def test_direct_access_still_denied_for_staff_non_superuser(self):
        staff = make_user(username="o2d_staff_direct", is_staff=True, is_superuser=False)
        client = Client()
        client.force_login(staff)

        resp = client.get(reverse("admin:broker_shadow_exposure"))

        self.assertEqual(resp.status_code, 403)

    def test_direct_access_still_allowed_for_superuser(self):
        superuser = make_user(username="o2d_super_direct", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)

        resp = client.get(reverse("admin:broker_shadow_exposure"))

        self.assertEqual(resp.status_code, 200)

    def test_staff_gains_no_new_permission_flags(self):
        staff = make_user(username="o2d_staff_flags", is_staff=True, is_superuser=False)
        self.assertFalse(staff.is_superuser)
        self.assertTrue(staff.is_staff)


class PreservedEndpointsTests(TestCase):
    """Lo que ya existía y no debía tocarse sigue funcionando igual."""

    def setUp(self):
        superuser = make_user(username="o2d_super_preserved", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(superuser)

    def test_control_center_json_data_endpoint_still_works(self):
        resp = self.client.get(reverse("admin:brokerrevsnap_control_data"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_live_analytics_still_reachable(self):
        resp = self.client.get(reverse("admin:broker_live_analytics"))
        self.assertEqual(resp.status_code, 200)

    def test_dealing_desk_per_account_still_reachable(self):
        account = make_account()
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_tradingaccount_changelist_still_shows_desk_link(self):
        make_account()
        resp = self.client.get(reverse("admin:simulator_tradingaccount_changelist"))
        self.assertContains(resp, "→ Desk")
