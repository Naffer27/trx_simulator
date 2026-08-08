# simulator/tests/test_o4a1_treasury_admin_2fa_helper.py
"""
Microbloque O.4a-1 — Treasury Admin 2FA gate: helper + setting only.

This is the FIRST microbloque of O.4a (2FA obligatorio en Django Admin,
Fase 0 approved design). It adds exactly two things, neither of which is
wired into anything yet:

  1. simulator.two_factor.treasury_2fa_required(user) — a pure predicate.
  2. settings.TOTP_ADMIN_TREASURY_REQUIRED — a new, dedicated, env-driven
     flag (default False), independent of TOTP_STAFF_REQUIRED (O.4a Fase 0
     Decision 1 — NOT the same flag).

Nothing here calls treasury_2fa_required() from any view, decorator, or
MoneyBrokerAdminSite method — that wiring is O.4a-2. These tests only
prove the helper's own truth table and the setting's own default/env
behavior, in isolation.
"""
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings

from simulator.two_factor import treasury_2fa_required
from simulator.tests.factories import make_user


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


class TreasuryTwoFaRequiredHelperTests(TestCase):
    """Truth table for treasury_2fa_required(user) — O.4a Fase 0 §3/§4."""

    def test_anonymous_user_returns_false(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(treasury_2fa_required(AnonymousUser()))

    def test_authenticated_non_staff_returns_false(self):
        user = make_user(is_staff=False)
        self.assertFalse(treasury_2fa_required(user))

    def test_staff_without_any_treasury_permission_returns_false(self):
        user = make_user(is_staff=True)
        self.assertFalse(treasury_2fa_required(user))

    def test_staff_with_unrelated_permission_returns_false(self):
        user = make_user(is_staff=True)
        _grant(user, "view_treasuryoperationrequest")
        self.assertFalse(treasury_2fa_required(user))

    def test_superuser_returns_true_even_without_staff_flag_explicit(self):
        # is_staff is required by the first guard clause; a realistic
        # superuser always has is_staff=True in practice, but this proves
        # the is_superuser branch itself, not just has_perm()'s bypass.
        user = make_user(is_staff=True, is_superuser=True)
        self.assertTrue(treasury_2fa_required(user))

    def test_superuser_without_staff_flag_returns_false(self):
        # Defensive: is_staff=False must still block admin-gate relevance
        # regardless of is_superuser, mirroring the same guard clause
        # staff_require_2fa() already uses.
        user = make_user(is_staff=False, is_superuser=True)
        self.assertFalse(treasury_2fa_required(user))

    def test_can_submit_treasury_request_returns_true(self):
        user = make_user(is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.assertTrue(treasury_2fa_required(user))

    def test_can_review_treasury_request_returns_true(self):
        user = make_user(is_staff=True)
        _grant(user, "can_review_treasury_request")
        self.assertTrue(treasury_2fa_required(user))

    def test_can_execute_treasury_request_returns_true(self):
        user = make_user(is_staff=True)
        _grant(user, "can_execute_treasury_request")
        self.assertTrue(treasury_2fa_required(user))

    def test_can_recover_treasury_execution_returns_true(self):
        user = make_user(is_staff=True)
        _grant(user, "can_recover_treasury_execution")
        self.assertTrue(treasury_2fa_required(user))

    def test_multiple_treasury_permissions_returns_true(self):
        user = make_user(is_staff=True)
        _grant(user, "can_submit_treasury_request")
        _grant(user, "can_review_treasury_request")
        self.assertTrue(treasury_2fa_required(user))

    def test_never_raises_for_any_input_combination(self):
        # Defensive shape parity with staff_require_2fa()'s own check —
        # no exception for any of the boundary combinations above.
        for is_staff in (True, False):
            for is_superuser in (True, False):
                with self.subTest(is_staff=is_staff, is_superuser=is_superuser):
                    user = make_user(is_staff=is_staff, is_superuser=is_superuser)
                    treasury_2fa_required(user)  # must not raise


class TotpAdminTreasuryRequiredSettingTests(TestCase):
    """settings.TOTP_ADMIN_TREASURY_REQUIRED — default and override behavior."""

    def test_default_is_false(self):
        from django.conf import settings
        self.assertFalse(settings.TOTP_ADMIN_TREASURY_REQUIRED)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_can_be_overridden_true(self):
        from django.conf import settings
        self.assertTrue(settings.TOTP_ADMIN_TREASURY_REQUIRED)

    def test_independent_of_totp_staff_required(self):
        # Flipping one flag must never move the other — O.4a Fase 0
        # Decision 1: dedicated, independent flag, not a TOTP_STAFF_REQUIRED
        # alias.
        from django.conf import settings
        with override_settings(TOTP_STAFF_REQUIRED=True, TOTP_ADMIN_TREASURY_REQUIRED=False):
            self.assertTrue(settings.TOTP_STAFF_REQUIRED)
            self.assertFalse(settings.TOTP_ADMIN_TREASURY_REQUIRED)
        with override_settings(TOTP_STAFF_REQUIRED=False, TOTP_ADMIN_TREASURY_REQUIRED=True):
            self.assertFalse(settings.TOTP_STAFF_REQUIRED)
            self.assertTrue(settings.TOTP_ADMIN_TREASURY_REQUIRED)


class NotYetWiredAnywhereTests(TestCase):
    """
    Scope guard from O.4a-1 — this class originally asserted the gate was
    NOT wired into MoneyBrokerAdminSite yet. That was true when O.4a-1
    was approved and closed (this microbloque only added the helper and
    the setting, with zero callers). O.4a-2 legitimately superseded it on
    purpose by wiring treasury_2fa_required() into
    MoneyBrokerAdminSite.admin_view() — the same "forward-looking guard,
    updated only when a later approved block legitimately supersedes it"
    pattern used throughout O.3 (O.3a-5's URL guard, updated 5 times by
    O.3b-3/O.3c-5a/O.3c-5b/O.3d-4/O.3e-3; O.3e-1's event-catalog guard,
    superseded by O.3e-2). This is not a defect — it is the guard
    correctly falsified by the next authorized block, exactly as
    expected.
    """

    def test_money_broker_admin_site_now_overrides_admin_view(self):
        from simulator.admin import MoneyBrokerAdminSite
        from django.contrib.admin import AdminSite
        self.assertIsNot(
            MoneyBrokerAdminSite.admin_view, AdminSite.admin_view,
            "O.4a-2 wires the Treasury 2FA gate via an admin_view() override "
            "— it must differ from AdminSite's own now.",
        )

    def test_treasury_2fa_required_is_called_from_admin_view_only(self):
        import ast
        import inspect
        import textwrap

        import simulator.admin as admin_module
        import simulator.two_factor as two_factor_module

        def _calls_treasury_2fa_required(fn):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if name == "treasury_2fa_required":
                        return True
            return False

        from simulator.admin import MoneyBrokerAdminSite

        self.assertTrue(
            _calls_treasury_2fa_required(MoneyBrokerAdminSite.admin_view),
            "MoneyBrokerAdminSite.admin_view() must call treasury_2fa_required() "
            "as of O.4a-2.",
        )

        # two_factor.py itself only DEFINES treasury_2fa_required() — it
        # must not also call itself or gain some other internal caller.
        source = textwrap.dedent(inspect.getsource(two_factor_module))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                self.assertNotEqual(
                    name, "treasury_2fa_required",
                    "two_factor.py must not call treasury_2fa_required() internally.",
                )
