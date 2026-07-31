# simulator/tests/test_o2g1b_treasury_sidebar.py
"""
Bloque O.2g-1b — Treasury Sidebar Organization.

Covers ONLY the single change this block makes: adding
"treasuryoperationrequest" to the existing TREASURY section of
MoneyBrokerAdminSite._SECTIONS (simulator/admin.py, ADMIN_UI.1 block),
so TreasuryOperationRequest groups with Wallet / WalletTransaction /
InternalTransfer in the admin sidebar instead of falling into the
UNCATEGORIZED safety-net section.

No model, migration, permission, view, URL or business logic is touched.
This is a pure sidebar-grouping change to get_app_list().
"""
from django.contrib import admin
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from .factories import make_user


class TreasurySidebarSectionTests(TestCase):

    def setUp(self):
        self.superuser = make_user(username="o2g1b_super", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)

    def _get_app_list(self):
        request = RequestFactory().get(reverse("admin:index"))
        request.user = self.superuser
        return admin.site.get_app_list(request)

    def test_treasuryoperationrequest_is_grouped_under_treasury(self):
        app_list = self._get_app_list()

        treasury = next((a for a in app_list if a["name"] == "TREASURY"), None)
        self.assertIsNotNone(treasury, "TREASURY section must still exist")

        model_names = {m["object_name"] for m in treasury["models"]}
        self.assertIn("TreasuryOperationRequest", model_names)
        self.assertIn("Wallet", model_names)
        self.assertIn("WalletTransaction", model_names)
        self.assertIn("InternalTransfer", model_names)

    def test_treasuryoperationrequest_is_not_in_uncategorized(self):
        app_list = self._get_app_list()

        uncategorized = next((a for a in app_list if a["name"] == "UNCATEGORIZED"), None)
        if uncategorized is not None:
            model_names = {m["object_name"] for m in uncategorized["models"]}
            self.assertNotIn("TreasuryOperationRequest", model_names)

    def test_admin_index_page_renders_with_treasury_section(self):
        resp = self.client.get(reverse("admin:index"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TREASURY")

    def test_no_other_sections_or_models_were_touched(self):
        app_list = self._get_app_list()

        section_names = {a["name"] for a in app_list}
        expected = {
            "Authentication and Authorization",
            "CORE OPERATIONS", "COMPLIANCE", "TRADING ENGINE",
            "FUNDING PROGRAMS", "PAYMENTS & LEDGER", "BROKER OPERATIONS",
            "TREASURY", "BROKER BUSINESS", "GROWTH", "TOOLS",
        }
        self.assertTrue(expected.issubset(section_names))
