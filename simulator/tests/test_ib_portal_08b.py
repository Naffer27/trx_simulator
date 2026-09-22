# simulator/tests/test_ib_portal_08b.py
"""
IB-PORTAL-08B — Lotage & Payout Transparency.

Regression coverage for:
  - simulator/views.py::associates_view() (the IB-facing /associates/ portal)
  - simulator/ib_admin_ops.py's new helpers: ib_commission_summary(),
    ib_obligation_status_label(), change_commission_rate(), and the new
    ib_referral_change_rate_view() admin action
  - simulator/admin.py::ReferralAdmin's hardened readonly_fields

Approved design: IB-PORTAL-08A audit + design lock. This suite proves:
  - the portal reads exclusively from the real SSOT (LotExecutionEvent,
    IBCommissionObligation, IBCommissionRule via resolve_applicable_
    rule()) — never Referral.estimated_commission (confirmed dead by
    08A), never a second calculation.
  - held is a breakdown of pending, never double-counted alongside it.
  - historical rows use their own permanently-snapshotted
    applied_fixed_rate/calculated_amount — never a re-resolved current
    rate.
  - Change Commission Rate creates a NEW IBCommissionRule row and closes
    the old one via effective_until — never edits an existing row's
    economic fields, never touches the GLOBAL rule when creating a
    per-IB override, never mutates any existing IBCommissionObligation.
  - ReferralAdmin can no longer be used to bypass freeze_referral()/
    unfreeze_referral() (IB-RISK-HOLDS-07B) or the dead estimated_
    commission field.
  - no IDOR: an IB never sees another IB's data via /associates/.
  - CPA_BONUS/SPREAD_REVENUE_SHARE remain untouched/unreferenced.
"""
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from simulator.ib_admin_ops import (
    CommissionRateUnchanged, InvalidCommissionRate, change_commission_rate,
    ib_commission_summary, ib_obligation_status_label,
)
from simulator.ib_risk_holds import freeze_referral, hold_obligation, unfreeze_referral
from simulator.models import (
    IBCommissionAdjustment, IBCommissionObligation, IBCommissionRule, LotExecutionEvent,
    Referral, ReferralAttribution, AuditLog, BrokerAuditEvent,
)
from simulator.tests.factories import make_account, make_user

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"portal08b_{_seq}"


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type=IBCommissionRule.RULE_PER_LOT, referral=None, enabled=True,
               fixed_amount=Decimal("8.00"), percentage=None, effective_from=None,
               effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=fixed_amount, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
        effective_until=effective_until,
    )


def _make_lot_event(account, qty="0.50", created_at=None):
    ev = LotExecutionEvent.objects.create(
        account=account, position=None, symbol="EUR/USD", side="BUY",
        qty=Decimal(qty), execution_price=Decimal("1.10000"),
        merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
    )
    if created_at is not None:
        LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
        ev.refresh_from_db()
    return ev


def _make_obligation(referral, rule, attribution=None, calculated_amount="10.00",
                      basis_quantity=None, applied_fixed_rate=None,
                      status=IBCommissionObligation.ST_PENDING, is_held=False,
                      source_event_id=None):
    if attribution is None:
        trader = make_user()
        attribution = _make_attribution(trader, referral)
    return IBCommissionObligation.objects.create(
        attribution=attribution, referral=referral, rule=rule, rule_type=rule.rule_type,
        source_event_type="test_event",
        source_event_id=source_event_id if source_event_id is not None else _next_seq(),
        basis_quantity=basis_quantity, applied_fixed_rate=applied_fixed_rate,
        calculated_amount=Decimal(calculated_amount), currency="USD", status=status,
        is_held=is_held,
    )


def _grant(user, codename):
    perm = Permission.objects.get(codename=codename)
    user.user_permissions.add(perm)
    user.refresh_from_db()
    return user


def _make_reviewer(**kwargs):
    return _grant(make_user(is_staff=True, **kwargs), "can_review_treasury_request")


def _fake_request(user):
    request = RequestFactory().post("/")
    request.user = user
    return request


def _associates_url():
    return reverse("simulator:associates")


def _change_rate_url(referral_id):
    return reverse("admin:ib_referral_change_rate", args=[referral_id])


def _ib_detail_url(referral_id):
    return reverse("admin:ib_detail", args=[referral_id])


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 1/2/3/4/20 — access, scope, clicks, registrations, IDOR
# ─────────────────────────────────────────────────────────────────────────

class PortalAccessTests(TestCase):
    def test_login_required(self):
        resp = self.client.get(_associates_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url.lower())

    def test_scoped_to_own_referral(self):
        owner = make_user()
        ref = _make_referral(owner)
        other_ref = _make_referral()
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.context["referral"].pk, ref.pk)
        self.assertNotEqual(resp.context["referral"].pk, other_ref.pk)

    def test_clicks_correct(self):
        owner = make_user()
        ref = _make_referral(owner)
        ref.clicks = 42
        ref.save(update_fields=["clicks"])
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertContains(resp, "42")

    def test_registrations_correct(self):
        owner = make_user()
        ref = _make_referral(owner)
        _make_attribution(make_user(), ref)
        _make_attribution(make_user(), ref)
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.context["referral"].attributions.count(), 2)

    def test_no_idor_via_page_param(self):
        """No ID of any kind is ever accepted from GET to change scope —
        confirm a second user's own portal never reflects the first
        user's referral no matter what GET params are sent."""
        owner_a = make_user()
        ref_a = _make_referral(owner_a)
        _make_rule(referral=ref_a, fixed_amount=Decimal("99.00"))
        owner_b = make_user()
        ref_b = _make_referral(owner_b)

        client = Client()
        client.force_login(owner_b)
        resp = client.get(_associates_url(), {"referral_id": ref_a.pk, "id": ref_a.pk, "page": 1})
        self.assertEqual(resp.context["referral"].pk, ref_b.pk)
        self.assertNotContains(resp, "99.00")


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 5 — lotage from SSOT
# ─────────────────────────────────────────────────────────────────────────

class PortalLotageTests(TestCase):
    def test_lots_from_ssot(self):
        owner = make_user()
        ref = _make_referral(owner)
        trader = make_user()
        _make_attribution(trader, ref)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_lot_event(account, qty="1.50")
        _make_lot_event(account, qty="2.00")

        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.context["lot_totals"]["lifetime"], Decimal("3.50"))


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 6/7 — current rate / "No configurada"
# ─────────────────────────────────────────────────────────────────────────

class PortalRateTests(TestCase):
    def test_current_rate_correct(self):
        owner = make_user()
        ref = _make_referral(owner)
        _make_rule(referral=ref, fixed_amount=Decimal("8.00"))
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.context["current_rate"], Decimal("8.00"))

    def test_no_rule_shows_not_configured(self):
        owner = make_user()
        _make_referral(owner)
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertIsNone(resp.context["current_rate"])
        # IB-PORTAL-UX-10A moved this into the new Commission Overview
        # section and re-labeled it in English (matching the reference's
        # UI language) — "Not configured", not the old "No configurada".
        # The underlying data contract (current_rate is None) is unchanged.
        self.assertContains(resp, "Not configured")


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 8/9/10/11/12/13 — commission summary semantics
# ─────────────────────────────────────────────────────────────────────────

class PortalCommissionSummaryTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = _make_referral(self.owner)
        self.rule = _make_rule(referral=self.ref)

    def test_generated_correct(self):
        _make_obligation(self.ref, self.rule, calculated_amount="10.00", status=IBCommissionObligation.ST_PENDING)
        _make_obligation(self.ref, self.rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)
        summary = ib_commission_summary(self.ref)
        self.assertEqual(summary["generated"], Decimal("30.00"))

    def test_pending_correct(self):
        _make_obligation(self.ref, self.rule, calculated_amount="10.00", status=IBCommissionObligation.ST_PENDING)
        _make_obligation(self.ref, self.rule, calculated_amount="5.00", status=IBCommissionObligation.ST_APPROVED)
        _make_obligation(self.ref, self.rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)
        summary = ib_commission_summary(self.ref)
        self.assertEqual(summary["pending"], Decimal("15.00"))

    def test_held_correct(self):
        _make_obligation(self.ref, self.rule, calculated_amount="10.00", status=IBCommissionObligation.ST_PENDING, is_held=True)
        _make_obligation(self.ref, self.rule, calculated_amount="5.00", status=IBCommissionObligation.ST_PENDING, is_held=False)
        summary = ib_commission_summary(self.ref)
        self.assertEqual(summary["held"], Decimal("10.00"))

    def test_held_is_not_double_counted(self):
        """held must be a SUBSET of pending, never additive — generated
        must equal pending + credited, not pending + held + credited."""
        _make_obligation(self.ref, self.rule, calculated_amount="10.00", status=IBCommissionObligation.ST_PENDING, is_held=True)
        _make_obligation(self.ref, self.rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)
        summary = ib_commission_summary(self.ref)
        self.assertEqual(summary["generated"], summary["pending"] + summary["credited"])
        self.assertLessEqual(summary["held"], summary["pending"])

    def test_credited_correct(self):
        _make_obligation(self.ref, self.rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)
        _make_obligation(self.ref, self.rule, calculated_amount="5.00", status=IBCommissionObligation.ST_PENDING)
        summary = ib_commission_summary(self.ref)
        self.assertEqual(summary["credited"], Decimal("20.00"))

    def test_estimated_commission_legacy_not_used(self):
        self.ref.estimated_commission = Decimal("9999.99")
        self.ref.save(update_fields=["estimated_commission"])
        _make_obligation(self.ref, self.rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)

        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        self.assertNotContains(resp, "9999.99")
        self.assertEqual(resp.context["commission_summary"]["credited"], Decimal("20.00"))


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 14/15 — frozen UX
# ─────────────────────────────────────────────────────────────────────────

class PortalFrozenTests(TestCase):
    def test_frozen_banner_shown(self):
        owner = make_user()
        ref = _make_referral(owner)
        freeze_referral(ref, "test freeze", request=_fake_request(_make_reviewer()))
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertContains(resp, "congelado")
        self.assertTrue(resp.context["is_frozen"])

    def test_frozen_ib_still_sees_history(self):
        owner = make_user()
        ref = _make_referral(owner)
        rule = _make_rule(referral=ref)
        _make_obligation(ref, rule, calculated_amount="20.00", status=IBCommissionObligation.ST_CREDITED)
        freeze_referral(ref, "test freeze", request=_fake_request(_make_reviewer()))

        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["history_rows"]), 1)
        self.assertEqual(resp.context["commission_summary"]["credited"], Decimal("20.00"))

    def test_frozen_banner_does_not_reveal_internal_reason(self):
        owner = make_user()
        ref = _make_referral(owner)
        freeze_referral(ref, "internal fraud investigation notes", request=_fake_request(_make_reviewer()))
        client = Client()
        client.force_login(owner)
        resp = client.get(_associates_url())
        self.assertNotContains(resp, "internal fraud investigation notes")


# ─────────────────────────────────────────────────────────────────────────
# PORTAL — 16/17/18/19 — history: pagination, historical snapshot, PII
# ─────────────────────────────────────────────────────────────────────────

class PortalHistoryTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.ref = _make_referral(self.owner)
        self.rule = _make_rule(referral=self.ref, fixed_amount=Decimal("8.00"))

    def test_history_paginated(self):
        for i in range(25):
            _make_obligation(self.ref, self.rule, calculated_amount="1.00", source_event_id=i)
        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        self.assertEqual(resp.context["history_page"].paginator.num_pages, 2)
        self.assertEqual(len(resp.context["history_rows"]), 20)

        resp2 = client.get(_associates_url(), {"page": 2})
        self.assertEqual(len(resp2.context["history_rows"]), 5)

    def test_historical_rate_uses_applied_fixed_rate_not_current(self):
        """Snapshot immutability (IB-TREASURY-CREDIT-03, reused): even
        if the rule changes later, a historical obligation's own
        applied_fixed_rate must be shown, never the NOW-current rate."""
        old_obligation = _make_obligation(
            self.ref, self.rule, calculated_amount="4.00",
            basis_quantity=Decimal("0.50"), applied_fixed_rate=Decimal("8.00"),
        )
        # Change the rate AFTER the obligation was generated.
        change_commission_rate(self.ref, "10.00", request=_fake_request(_make_reviewer()))

        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        row = next(r for r in resp.context["history_rows"] if r["obligation"].pk == old_obligation.pk)
        self.assertEqual(row["rate_label"], "$8.00/lot")
        self.assertEqual(resp.context["current_rate"], Decimal("10.00"))

    def test_historical_amount_uses_calculated_amount(self):
        obligation = _make_obligation(
            self.ref, self.rule, calculated_amount="4.00",
            basis_quantity=Decimal("0.50"), applied_fixed_rate=Decimal("8.00"),
        )
        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        row = next(r for r in resp.context["history_rows"] if r["obligation"].pk == obligation.pk)
        self.assertEqual(row["obligation"].calculated_amount, Decimal("4.00"))

    def test_lots_shown_only_when_certified_not_derived(self):
        """PER_LOT row: basis_quantity IS the certified source-event
        snapshot — shown as-is. A percentage-type row (basis_quantity
        never set by those generators) must show '—', never a division
        of calculated_amount/rate."""
        per_lot_ob = _make_obligation(
            self.ref, self.rule, calculated_amount="4.00", basis_quantity=Decimal("0.50"),
        )
        percent_rule = _make_rule(
            rule_type=IBCommissionRule.RULE_DEPOSIT_PERCENT, referral=self.ref,
            fixed_amount=None, percentage=Decimal("5.00"),
        )
        percent_ob = IBCommissionObligation.objects.create(
            attribution=per_lot_ob.attribution, referral=self.ref, rule=percent_rule,
            rule_type=percent_rule.rule_type, source_event_type="test_event",
            source_event_id=_next_seq(), basis_amount=Decimal("100.00"),
            applied_percentage_rate=Decimal("5.00"), calculated_amount=Decimal("5.00"),
            currency="USD", status=IBCommissionObligation.ST_PENDING,
        )
        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        rows_by_pk = {r["obligation"].pk: r for r in resp.context["history_rows"]}
        self.assertEqual(rows_by_pk[per_lot_ob.pk]["lots"], Decimal("0.50"))
        self.assertIsNone(rows_by_pk[percent_ob.pk]["lots"])

    def test_no_pii_leakage(self):
        trader = make_user(email="trader-secret@example.com")
        attribution = _make_attribution(trader, self.ref)
        _make_obligation(self.ref, self.rule, attribution=attribution, calculated_amount="4.00")
        client = Client()
        client.force_login(self.owner)
        resp = client.get(_associates_url())
        self.assertNotContains(resp, "trader-secret@example.com")
        self.assertContains(resp, trader.username)


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 21/22/23 — ib_detail shows rate/lots/commission totals
# ─────────────────────────────────────────────────────────────────────────

class AdminIBDetailTests(TestCase):
    def setUp(self):
        self.reviewer = _make_reviewer()
        self.client = Client()
        self.client.force_login(self.reviewer)
        self.owner = make_user()
        self.ref = _make_referral(self.owner)
        self.rule = _make_rule(referral=self.ref, fixed_amount=Decimal("8.00"))

    def test_ib_detail_shows_rate(self):
        resp = self.client.get(_ib_detail_url(self.ref.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "8.00")

    def test_ib_detail_shows_lots(self):
        trader = make_user()
        _make_attribution(trader, self.ref)
        account = make_account(user=trader, balance=Decimal("10000"))
        _make_lot_event(account, qty="3.00")
        resp = self.client.get(_ib_detail_url(self.ref.pk))
        self.assertEqual(resp.context["lot_totals"]["lifetime"], Decimal("3.00"))

    def test_ib_detail_shows_commission_totals(self):
        _make_obligation(self.ref, self.rule, calculated_amount="15.00", status=IBCommissionObligation.ST_CREDITED)
        resp = self.client.get(_ib_detail_url(self.ref.pk))
        self.assertEqual(resp.context["commission_summary"]["credited"], Decimal("15.00"))


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 24/25/26/27/28/29/30/31/32/38 — Change Commission Rate
# ─────────────────────────────────────────────────────────────────────────

class ChangeRateViewTests(TestCase):
    def setUp(self):
        self.reviewer = _make_reviewer()
        self.client = Client()
        self.client.force_login(self.reviewer)
        self.owner = make_user()
        self.ref = _make_referral(self.owner)
        self.old_rule = _make_rule(referral=self.ref, fixed_amount=Decimal("8.00"))

    def test_get_does_not_mutate(self):
        resp = self.client.get(_change_rate_url(self.ref.pk))
        self.assertEqual(resp.status_code, 200)
        self.old_rule.refresh_from_db()
        self.assertIsNone(self.old_rule.effective_until)
        self.assertEqual(IBCommissionRule.objects.filter(referral=self.ref).count(), 1)

    def test_requires_permission(self):
        plain = make_user(is_staff=True)
        client = Client()
        client.force_login(plain)
        resp = client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        self.assertEqual(resp.status_code, 403)

    def test_post_csrf_compatible(self):
        """Django's test Client is CSRF-exempt by default, matching this
        whole program's established admin-view test convention (see
        test_ib_risk_holds_07b.py's AdminViewTests) — the form itself
        carries {% csrf_token %} (checked structurally elsewhere); a
        302/200 (not 403) here confirms the view path itself is intact."""
        resp = self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"}, follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_change_creates_new_rule(self):
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        new_rule = IBCommissionRule.objects.get(referral=self.ref, effective_until__isnull=True)
        self.assertEqual(new_rule.fixed_amount, Decimal("10.00"))
        self.assertNotEqual(new_rule.pk, self.old_rule.pk)

    def test_old_rule_remains_historical_not_deleted(self):
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        self.assertTrue(IBCommissionRule.objects.filter(pk=self.old_rule.pk).exists())

    def test_old_rule_closed_correctly(self):
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        self.old_rule.refresh_from_db()
        self.assertIsNotNone(self.old_rule.effective_until)
        self.assertEqual(self.old_rule.fixed_amount, Decimal("8.00"))  # economic field untouched

    def test_historical_obligations_unchanged(self):
        obligation = _make_obligation(
            self.ref, self.old_rule, calculated_amount="4.00",
            basis_quantity=Decimal("0.50"), applied_fixed_rate=Decimal("8.00"),
            status=IBCommissionObligation.ST_CREDITED,
        )
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, Decimal("4.00"))
        self.assertEqual(obligation.status, IBCommissionObligation.ST_CREDITED)

    def test_applied_fixed_rate_historical_unchanged(self):
        obligation = _make_obligation(
            self.ref, self.old_rule, calculated_amount="4.00", applied_fixed_rate=Decimal("8.00"),
        )
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        obligation.refresh_from_db()
        self.assertEqual(obligation.applied_fixed_rate, Decimal("8.00"))

    def test_calculated_amount_historical_unchanged(self):
        obligation = _make_obligation(self.ref, self.old_rule, calculated_amount="4.00")
        self.client.post(_change_rate_url(self.ref.pk), {"new_fixed_amount": "10.00"})
        obligation.refresh_from_db()
        self.assertEqual(obligation.calculated_amount, Decimal("4.00"))

    def test_invalid_rate_rejected_zero(self):
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(self.ref, "0", request=_fake_request(self.reviewer))

    def test_invalid_rate_rejected_negative(self):
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(self.ref, "-5.00", request=_fake_request(self.reviewer))

    def test_invalid_rate_rejected_garbage(self):
        with self.assertRaises(InvalidCommissionRate):
            change_commission_rate(self.ref, "not-a-number", request=_fake_request(self.reviewer))

    def test_same_rate_rejected_as_noop(self):
        with self.assertRaises(CommissionRateUnchanged):
            change_commission_rate(self.ref, "8.00", request=_fake_request(self.reviewer))

    def test_first_time_override_no_prior_rule(self):
        ref2 = _make_referral()
        new_rule = change_commission_rate(ref2, "12.00", request=_fake_request(self.reviewer))
        self.assertEqual(new_rule.fixed_amount, Decimal("12.00"))
        self.assertEqual(new_rule.referral_id, ref2.pk)


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 33/34/35 — global-rule protection
# ─────────────────────────────────────────────────────────────────────────

class GlobalRuleProtectionTests(TestCase):
    def test_global_rule_not_modified_when_creating_ib_override(self):
        global_rule = _make_rule(referral=None, fixed_amount=Decimal("5.00"))
        ref = _make_referral()
        reviewer = _make_reviewer()

        change_commission_rate(ref, "9.00", request=_fake_request(reviewer))

        global_rule.refresh_from_db()
        self.assertIsNone(global_rule.effective_until)
        self.assertEqual(global_rule.fixed_amount, Decimal("5.00"))

    def test_new_override_applies_only_to_that_ib(self):
        _make_rule(referral=None, fixed_amount=Decimal("5.00"))
        ref_a = _make_referral()
        ref_b = _make_referral()
        reviewer = _make_reviewer()

        change_commission_rate(ref_a, "9.00", request=_fake_request(reviewer))

        from simulator.ib_admin_ops import ib_effective_rate
        rule_a, source_a = ib_effective_rate(ref_a, IBCommissionRule.RULE_PER_LOT)
        rule_b, source_b = ib_effective_rate(ref_b, IBCommissionRule.RULE_PER_LOT)
        self.assertEqual(rule_a.fixed_amount, Decimal("9.00"))
        self.assertEqual(source_a, "per-IB")
        self.assertEqual(rule_b.fixed_amount, Decimal("5.00"))
        self.assertEqual(source_b, "global")

    def test_other_ib_keeps_its_own_rule(self):
        ref_a = _make_referral()
        ref_b = _make_referral()
        _make_rule(referral=ref_a, fixed_amount=Decimal("7.00"))
        rule_b = _make_rule(referral=ref_b, fixed_amount=Decimal("11.00"))
        reviewer = _make_reviewer()

        change_commission_rate(ref_a, "20.00", request=_fake_request(reviewer))

        rule_b.refresh_from_db()
        self.assertIsNone(rule_b.effective_until)
        self.assertEqual(rule_b.fixed_amount, Decimal("11.00"))


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 36 — concurrency / idempotency (structural + functional —
# see this suite's own report for why a live-thread test was avoided:
# this codebase already has one known-flaky live-thread concurrency
# test (test_fix02a3_admin_hardening.py), so a functional-serialization
# proof + a structural select_for_update() grep is used instead, per
# the SQLite-vs-PostgreSQL boundary disclosed throughout this session).
# ─────────────────────────────────────────────────────────────────────────

class ConcurrencyIdempotencyTests(TestCase):
    def test_sequential_rate_changes_never_leave_two_open_rules(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        _make_rule(referral=ref, fixed_amount=Decimal("5.00"))

        change_commission_rate(ref, "6.00", request=_fake_request(reviewer))
        change_commission_rate(ref, "7.00", request=_fake_request(reviewer))
        change_commission_rate(ref, "8.00", request=_fake_request(reviewer))

        open_rules = IBCommissionRule.objects.filter(
            rule_type=IBCommissionRule.RULE_PER_LOT, referral=ref, effective_until__isnull=True,
        )
        self.assertEqual(open_rules.count(), 1)
        self.assertEqual(open_rules.first().fixed_amount, Decimal("8.00"))
        self.assertEqual(
            IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_PER_LOT, referral=ref).count(),
            4,  # 1 initial + 3 changes, all closed except the last
        )

    def test_change_commission_rate_uses_select_for_update(self):
        import inspect

        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops.change_commission_rate)
        self.assertIn("select_for_update", source)


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 37 — audit log created
# ─────────────────────────────────────────────────────────────────────────

class AuditLogTests(TestCase):
    def test_rate_change_writes_audit_log(self):
        ref = _make_referral()
        _make_rule(referral=ref, fixed_amount=Decimal("5.00"))
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        client.post(_change_rate_url(ref.pk), {"new_fixed_amount": "9.00"})
        self.assertTrue(AuditLog.objects.filter(event_type="ib_admin.rate_changed").exists())
        self.assertTrue(BrokerAuditEvent.objects.filter(event_type="ib_admin.rate_changed").exists())


# ─────────────────────────────────────────────────────────────────────────
# ADMIN — 39 — Freeze/Unfreeze (07B) still works
# ─────────────────────────────────────────────────────────────────────────

class FreezeStillWorksTests(TestCase):
    def test_freeze_unfreeze_roundtrip_still_works(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        frozen = freeze_referral(ref, "x", request=_fake_request(reviewer))
        self.assertEqual(frozen.risk_status, Referral.RISK_FROZEN)
        unfrozen = unfreeze_referral(ref, "y", request=_fake_request(reviewer))
        self.assertEqual(unfrozen.risk_status, Referral.RISK_ACTIVE)

    def test_ib_detail_freeze_button_still_present(self):
        ref = _make_referral()
        reviewer = _make_reviewer()
        client = Client()
        client.force_login(reviewer)
        resp = client.get(_ib_detail_url(ref.pk))
        self.assertTrue(resp.context["show_ib_freeze_button"])


# ─────────────────────────────────────────────────────────────────────────
# ADMIN HARDENING — 40/41/42/43/44 — ReferralAdmin readonly fields
# ─────────────────────────────────────────────────────────────────────────

class ReferralAdminHardeningTests(TestCase):
    def setUp(self):
        from simulator.admin import ReferralAdmin
        self.model_admin = ReferralAdmin(Referral, None)

    def test_risk_status_readonly(self):
        self.assertIn("risk_status", self.model_admin.readonly_fields)

    def test_frozen_at_readonly(self):
        self.assertIn("frozen_at", self.model_admin.readonly_fields)

    def test_frozen_by_readonly(self):
        self.assertIn("frozen_by", self.model_admin.readonly_fields)

    def test_frozen_reason_readonly(self):
        self.assertIn("frozen_reason", self.model_admin.readonly_fields)

    def test_estimated_commission_readonly(self):
        self.assertIn("estimated_commission", self.model_admin.readonly_fields)

    def test_referral_change_form_cannot_bypass_freeze(self):
        """End-to-end proof, not just a readonly_fields list check: POST
        a raw change-form submission trying to flip risk_status directly
        — it must NOT take effect, since risk_status is readonly."""
        ref = _make_referral()
        superuser = make_user(is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)
        url = reverse("admin:simulator_referral_change", args=[ref.pk])
        get_resp = client.get(url)
        self.assertEqual(get_resp.status_code, 200)
        client.post(url, {
            "user": ref.user_id, "risk_status": Referral.RISK_FROZEN,
            "estimated_commission": "9999.99",
        })
        ref.refresh_from_db()
        self.assertEqual(ref.risk_status, Referral.RISK_ACTIVE)
        self.assertEqual(ref.estimated_commission, Decimal("0"))


# ─────────────────────────────────────────────────────────────────────────
# STRUCTURAL — 45/46/47/48/49
# ─────────────────────────────────────────────────────────────────────────

class StructuralTests(TestCase):
    def test_no_lotage_mutation_surface_in_ib_admin_ops(self):
        import inspect

        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops)
        self.assertNotIn("LotExecutionEvent.objects.create", source)
        self.assertNotIn("LotExecutionEvent.objects.update", source)
        self.assertNotIn(".qty =", source)

    def test_no_lotage_mutation_surface_in_views(self):
        import inspect

        from simulator import views
        source = inspect.getsource(views.associates_view)
        self.assertNotIn("LotExecutionEvent.objects.create", source)
        self.assertNotIn("LotExecutionEvent.objects.update", source)

    def test_no_second_lotage_ssot_portal_reuses_ib_lot_totals(self):
        """
        IB-PORTAL-UX-10A (authorized) added a period-parameterized
        "Traded Volume" card that legitimately sums LotExecutionEvent.qty
        directly — the exact same field ib_lot_totals()/
        ib_admin_ops._lot_sum_subquery() already sum internally, just with
        a caller-chosen period window instead of the fixed today/week/
        month/lifetime buckets ib_lot_totals() offers. That is reuse of
        the SSOT field, not a second one. What this test actually guards
        against is a DIFFERENT lot-notional formula being invented (e.g.
        price*qty/contract_size math, or reading lots from Trade/Position
        instead of the certified LotExecutionEvent anchor) — never a
        literal Sum('qty') call, which is the correct, authorized shape.
        """
        import inspect

        from simulator import views
        source = inspect.getsource(views.associates_view)
        self.assertIn("ib_lot_totals", source)
        # Every Sum('qty')-shaped aggregate in this view must be scoped to
        # LotExecutionEvent (the certified anchor) — never Trade/Position.
        self.assertNotIn("Trade.objects.aggregate", source)
        self.assertNotIn("Position.objects.aggregate", source)
        self.assertNotIn("qty * ", source)
        self.assertNotIn("qty*", source)

    def test_no_historical_recalculation_in_change_commission_rate(self):
        """The function's own docstring legitimately mentions calculated_
        amount/applied_fixed_rate/IBCommissionObligation in prose
        (explaining what it deliberately does NOT touch) — this checks
        for actual field-write/query patterns, not a bare substring
        match against that prose."""
        import inspect

        from simulator import ib_admin_ops
        source = inspect.getsource(ib_admin_ops.change_commission_rate)
        self.assertNotIn(".calculated_amount =", source)
        self.assertNotIn(".applied_fixed_rate =", source)
        self.assertNotIn(".applied_percentage_rate =", source)
        self.assertNotIn("IBCommissionObligation.objects", source)

    def test_no_cpa_bonus_activation(self):
        """change_commission_rate()'s own docstring legitimately mentions
        CPA_BONUS in prose (explaining it is never passed as rule_type
        by any caller) — checked here for an actual RULE_CPA_BONUS usage
        as a real argument/value, not a bare substring match."""
        self.assertEqual(
            IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_CPA_BONUS).count(), 0,
        )
        import inspect

        from simulator import ib_admin_ops, views
        for source in (inspect.getsource(views.associates_view), inspect.getsource(ib_admin_ops.change_commission_rate)):
            self.assertNotIn("RULE_CPA_BONUS", source)
            self.assertNotIn('"CPA_BONUS"', source)
            self.assertNotIn("'CPA_BONUS'", source)

    def test_no_spread_revenue_share_activation(self):
        self.assertEqual(
            IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_SPREAD_REVENUE_SHARE).count(), 0,
        )
        import inspect

        from simulator import ib_admin_ops, views
        for source in (inspect.getsource(views.associates_view), inspect.getsource(ib_admin_ops.change_commission_rate)):
            self.assertNotIn("RULE_SPREAD_REVENUE_SHARE", source)
            self.assertNotIn('"SPREAD_REVENUE_SHARE"', source)
            self.assertNotIn("'SPREAD_REVENUE_SHARE'", source)


# ─────────────────────────────────────────────────────────────────────────
# Extra edge cases discovered while implementing
# ─────────────────────────────────────────────────────────────────────────

class StatusLabelTests(TestCase):
    def setUp(self):
        self.ref = _make_referral()
        self.rule = _make_rule(referral=self.ref)

    def test_pending_label(self):
        ob = _make_obligation(self.ref, self.rule, status=IBCommissionObligation.ST_PENDING)
        self.assertEqual(ib_obligation_status_label(ob), "Pendiente de revisión")

    def test_approved_unlinked_label(self):
        ob = _make_obligation(self.ref, self.rule, status=IBCommissionObligation.ST_APPROVED)
        self.assertEqual(ib_obligation_status_label(ob), "Aprobada")

    def test_credited_label(self):
        ob = _make_obligation(self.ref, self.rule, status=IBCommissionObligation.ST_CREDITED)
        self.assertEqual(ib_obligation_status_label(ob), "Acreditada")

    def test_never_says_pagada(self):
        for status in (
            IBCommissionObligation.ST_PENDING, IBCommissionObligation.ST_APPROVED,
            IBCommissionObligation.ST_CREDITED,
        ):
            ob = _make_obligation(self.ref, self.rule, status=status, source_event_id=_next_seq())
            label = ib_obligation_status_label(ob)
            self.assertNotIn("Pagada", label)
            self.assertNotIn("Paid", label)


class PerformanceQueryBoundTests(TestCase):
    def test_commission_summary_bounded_queries(self):
        ref = _make_referral()
        rule = _make_rule(referral=ref)
        for i in range(30):
            _make_obligation(ref, rule, calculated_amount="1.00", source_event_id=i)
        with self.assertNumQueries(2):
            ib_commission_summary(ref)

    def test_portal_history_page_select_related_bounds_queries(self):
        """No N+1: querying 10 obligations (each with its own trader via
        attribution) must not scale linearly with row count — a fixed,
        bounded number of queries regardless of how many rows the page
        holds, proving select_related() is doing its job."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        owner = make_user()
        ref = _make_referral(owner)
        rule = _make_rule(referral=ref)
        for i in range(10):
            trader = make_user()
            attribution = _make_attribution(trader, ref)
            _make_obligation(ref, rule, attribution=attribution, calculated_amount="1.00", source_event_id=i)

        client = Client()
        client.force_login(owner)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(_associates_url())
        self.assertEqual(resp.status_code, 200)
        # Bounded, not proportional to the 10 obligations/traders above —
        # a regression to per-row queries (N+1) would push this well past
        # a small constant; this margin tolerates unrelated incidental
        # queries (session/auth/etc.) without being a brittle exact match.
        # IB-PORTAL-UX-10A raised the fixed baseline from ~24 to ~32 by
        # adding ~15 new real, O(1)-per-request aggregate queries (top-4
        # cards' current+previous counts, FTD grouped count, 4 day-
        # bucketed performance series, 3 financial-card aggregates) —
        # none of which scale with the 10 obligations/traders above (see
        # test_ib_portal_ux_10a.py's own dedicated scaling test, which
        # proves that directly). The threshold is widened, not removed,
        # so a genuine N+1 regression still fails this test loudly.
        self.assertLess(len(ctx.captured_queries), 45)