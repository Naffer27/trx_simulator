# simulator/tests/test_customer_support_01b_support_panel.py
"""
CUSTOMER-SUPPORT-01B — dedicated Support Panel, authorization,
status/assignment/escalation, and the permission-boundary proof that
SUPPORT never gains any financial capability.

AuditLog instrumentation is deliberately deferred to CUSTOMER-SUPPORT-01F
(see support_panel_views.py's module docstring) — not tested here for
that reason, not because it was forgotten.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from simulator.models import (
    OpsAdminProfile, OwnerRoot, SupportMessage, SupportTicket, WithdrawalRequest,
)
from simulator.owner_actions import owner_trading_account_adjustment, owner_wallet_adjustment
from simulator.support_status import IllegalTransition, apply_transition, legal_next_statuses
from .factories import make_account, make_user, make_wallet


def _support_permission():
    return Permission.objects.get(content_type__app_label="simulator", codename="is_customer_support")


def _make_owner(**kwargs):
    user = make_user(is_superuser=True, is_staff=True, **kwargs)
    OwnerRoot.objects.create(user=user, established_by="test")
    return user


def _make_ops(owner, **kwargs):
    user = make_user(is_staff=True, **kwargs)
    OpsAdminProfile.objects.create(user=user, assigned_by=owner)
    return user


def _make_support(**kwargs):
    user = make_user(is_staff=False, **kwargs)
    user.user_permissions.add(_support_permission())
    return user


def _make_ordinary_staff(**kwargs):
    return make_user(is_staff=True, **kwargs)


def _make_ticket(**kwargs):
    client = kwargs.pop("client_user", None) or make_user()
    defaults = dict(
        category=SupportTicket.CATEGORY_OTHER, subject="Help needed", message="Original message body",
    )
    defaults.update(kwargs)
    return SupportTicket.objects.create(user=client, **defaults)


QUEUE_URL = "/staff/support/"


def _detail_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/"


def _reply_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/reply/"


def _note_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/note/"


def _assign_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/assign/"


def _status_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/status/"


def _escalate_url(ticket):
    return f"/staff/support/tickets/{ticket.pk}/escalate/"


# ── Access control ───────────────────────────────────────────────────────

class AnonymousAccessTests(TestCase):
    def test_queue_redirects_to_login(self):
        resp = self.client.get(QUEUE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_detail_redirects_to_login(self):
        ticket = _make_ticket()
        resp = self.client.get(_detail_url(ticket))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])


class ClientDeniedTests(TestCase):
    def setUp(self):
        self.client_user = make_user()
        self.ticket = _make_ticket(client_user=self.client_user)

    def test_client_denied_queue(self):
        self.client.force_login(self.client_user)
        resp = self.client.get(QUEUE_URL)
        self.assertEqual(resp.status_code, 403)

    def test_client_denied_own_ticket_detail(self):
        self.client.force_login(self.client_user)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 403)


class OrdinaryStaffDeniedTests(TestCase):
    """is_staff=True alone must be insufficient — proves the panel
    authorizes exclusively via permission_levels.py, never is_staff."""

    def setUp(self):
        self.staff = _make_ordinary_staff()
        self.ticket = _make_ticket()

    def test_ordinary_staff_denied_queue(self):
        self.client.force_login(self.staff)
        resp = self.client.get(QUEUE_URL)
        self.assertEqual(resp.status_code, 403)

    def test_ordinary_staff_denied_detail(self):
        self.client.force_login(self.staff)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 403)


# ── SUPPORT capability tests ────────────────────────────────────────────

class SupportPanelAccessTests(TestCase):
    def setUp(self):
        self.support = _make_support()
        self.assertFalse(self.support.is_staff)
        self.ticket = _make_ticket()
        self.client.force_login(self.support)

    def test_support_can_view_queue(self):
        resp = self.client.get(QUEUE_URL)
        self.assertEqual(resp.status_code, 200)

    def test_support_can_view_detail(self):
        resp = self.client.get(_detail_url(self.ticket))
        self.assertEqual(resp.status_code, 200)

    def test_support_can_reply_customer_visible(self):
        resp = self.client.post(_reply_url(self.ticket), {"body": "We are looking into it"})
        self.assertEqual(resp.status_code, 302)
        msg = SupportMessage.objects.get(ticket=self.ticket)
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_CUSTOMER)
        self.assertEqual(msg.author_id, self.support.pk)
        self.assertEqual(msg.author_role, "SUPPORT")

    def test_support_can_post_internal_note(self):
        resp = self.client.post(_note_url(self.ticket), {"body": "internal only"})
        self.assertEqual(resp.status_code, 302)
        msg = SupportMessage.objects.get(ticket=self.ticket)
        self.assertEqual(msg.visibility, SupportMessage.VISIBILITY_INTERNAL)

    def test_support_can_claim_unassigned(self):
        resp = self.client.post(_assign_url(self.ticket), {"action": "claim"})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_id, self.support.pk)
        self.assertIsNotNone(self.ticket.assigned_at)

    def test_support_can_unclaim_own(self):
        self.client.post(_assign_url(self.ticket), {"action": "claim"})
        resp = self.client.post(_assign_url(self.ticket), {"action": "unclaim"})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)
        self.assertIsNone(self.ticket.assigned_at)

    def test_support_cannot_reassign_to_another_agent(self):
        other_support = _make_support(username="other_agent")
        resp = self.client.post(_assign_url(self.ticket), {"action": "reassign", "user_id": other_support.pk})
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)

    def test_support_cannot_unassign(self):
        self.client.post(_assign_url(self.ticket), {"action": "claim"})
        resp = self.client.post(_assign_url(self.ticket), {"action": "unassign"})
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_id, self.support.pk)

    def test_support_cannot_resolve_escalated(self):
        self.ticket.status = SupportTicket.STATUS_ESCALATED
        self.ticket.escalated_to_ops = True
        self.ticket.save()
        resp = self.client.post(_status_url(self.ticket), {"status": SupportTicket.STATUS_RESOLVED})
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_ESCALATED)

    def test_support_can_escalate(self):
        resp = self.client.post(_escalate_url(self.ticket), {"reason": "suspected fraud"})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_ESCALATED)
        self.assertTrue(self.ticket.escalated_to_ops)
        self.assertEqual(self.ticket.escalated_by_id, self.support.pk)


class SupportCannotAccessAdminTests(TestCase):
    def setUp(self):
        self.support = _make_support()

    def test_support_redirected_away_from_admin_index(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))

    def test_support_redirected_away_from_supportticket_admin(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/simulator/supportticket/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))

    def test_support_redirected_away_from_ownerroot_admin(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/simulator/ownerroot/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))

    def test_support_redirected_away_from_opsadminprofile_admin(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/simulator/opsadminprofile/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))

    def test_support_redirected_away_from_wallet_admin(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/simulator/wallet/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))

    def test_support_redirected_away_from_tradingaccount_admin(self):
        self.client.force_login(self.support)
        resp = self.client.get("/admin/simulator/tradingaccount/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/admin/login/"))


class SupportFinancialActionsDeniedTests(TestCase):
    """
    CUSTOMER-SUPPORT-01B §11 — explicit, direct-call evidence (not just
    UI-hidden buttons) that a SUPPORT-permission-holding actor is denied
    by the EXISTING financial systems, unmodified. These functions are
    never imported by support_panel_views.py in the first place; these
    tests additionally prove that even a direct call is refused.
    """

    def setUp(self):
        self.support = _make_support()
        self.account = make_account(account_type="RETAIL", balance=Decimal("500.00"))
        self.wallet = make_wallet(initial_balance=Decimal("200.00"))

    def test_support_cannot_owner_trading_account_adjustment(self):
        with self.assertRaises(PermissionDenied):
            owner_trading_account_adjustment(
                self.account.id, Decimal("50"), actor=self.support, reason="test",
                totp_code="000000", idempotency_key="sup-deny-ta-1",
            )

    def test_support_cannot_owner_wallet_adjustment(self):
        with self.assertRaises(PermissionDenied):
            owner_wallet_adjustment(
                self.wallet.id, Decimal("50"), actor=self.support, reason="test",
                totp_code="000000", idempotency_key="sup-deny-wa-1",
            )

    def test_support_has_no_treasury_permissions(self):
        from simulator.treasury_permissions import held_treasury_codenames
        self.assertEqual(held_treasury_codenames(self.support), ())

    def test_support_panel_module_imports_no_financial_code(self):
        import ast
        import simulator.support_panel_views as spv

        tree = ast.parse(open(spv.__file__).read())
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported_names.add(module)
                imported_names.update(alias.name for alias in node.names)

        forbidden = {
            "owner_actions", "wallet_ledger", "treasury_requests",
            "OwnerRoot", "OpsAdminProfile", "Wallet", "TradingAccount",
        }
        self.assertEqual(imported_names & forbidden, set())


# ── OPS capability tests ─────────────────────────────────────────────────

class OpsPanelTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.ops = _make_ops(self.owner)
        self.ticket = _make_ticket()
        self.client.force_login(self.ops)

    def test_ops_can_reassign(self):
        other_support = _make_support(username="agent_x")
        self.client.post(_assign_url(self.ticket), {"action": "claim"})  # unassigned first not required
        resp = self.client.post(_assign_url(self.ticket), {"action": "reassign", "user_id": other_support.pk})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_id, other_support.pk)

    def test_ops_can_unassign(self):
        self.client.post(_assign_url(self.ticket), {"action": "claim"})
        resp = self.client.post(_assign_url(self.ticket), {"action": "unassign"})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)

    def test_ops_can_resolve_escalated(self):
        self.ticket.status = SupportTicket.STATUS_ESCALATED
        self.ticket.escalated_to_ops = True
        self.ticket.save()
        resp = self.client.post(_status_url(self.ticket), {"status": SupportTicket.STATUS_RESOLVED})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_RESOLVED)
        self.assertIsNotNone(self.ticket.resolved_at)

    def test_ops_can_hand_back_escalated(self):
        self.ticket.status = SupportTicket.STATUS_ESCALATED
        self.ticket.escalated_to_ops = True
        self.ticket.save()
        resp = self.client.post(_status_url(self.ticket), {"status": SupportTicket.STATUS_PENDING_SUPPORT})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_PENDING_SUPPORT)
        # historical marker persists regardless of live status
        self.assertTrue(self.ticket.escalated_to_ops)

    def test_reassign_rejects_non_assignable_target(self):
        ordinary = make_user(username="plain_joe")
        resp = self.client.post(_assign_url(self.ticket), {"action": "reassign", "user_id": ordinary.pk})
        self.assertEqual(resp.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)


class OwnerPanelTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.ticket = _make_ticket()
        self.client.force_login(self.owner)

    def test_owner_full_authority_reassign_and_resolve_escalated(self):
        agent = _make_support()
        self.client.post(_assign_url(self.ticket), {"action": "reassign", "user_id": agent.pk})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_id, agent.pk)

        self.ticket.status = SupportTicket.STATUS_ESCALATED
        self.ticket.escalated_to_ops = True
        self.ticket.save()
        resp = self.client.post(_status_url(self.ticket), {"status": SupportTicket.STATUS_RESOLVED})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_RESOLVED)


# ── Status transition service ───────────────────────────────────────────

class StatusTransitionServiceTests(TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.ops = _make_ops(self.owner)
        self.support = _make_support()

    def test_open_to_pending_customer_legal_for_support(self):
        t = _make_ticket(status=SupportTicket.STATUS_OPEN)
        apply_transition(t, SupportTicket.STATUS_PENDING_CUSTOMER, actor=self.support)
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_CUSTOMER)

    def test_open_to_escalated_legal_for_support(self):
        t = _make_ticket(status=SupportTicket.STATUS_OPEN)
        apply_transition(t, SupportTicket.STATUS_ESCALATED, actor=self.support)
        self.assertEqual(t.status, SupportTicket.STATUS_ESCALATED)

    def test_escalated_to_resolved_illegal_for_support(self):
        t = _make_ticket(status=SupportTicket.STATUS_ESCALATED)
        with self.assertRaises(PermissionDenied):
            apply_transition(t, SupportTicket.STATUS_RESOLVED, actor=self.support)

    def test_escalated_to_resolved_legal_for_ops(self):
        t = _make_ticket(status=SupportTicket.STATUS_ESCALATED)
        apply_transition(t, SupportTicket.STATUS_RESOLVED, actor=self.ops)
        self.assertEqual(t.status, SupportTicket.STATUS_RESOLVED)

    def test_no_transition_targets_legacy_pending(self):
        for status in (
            SupportTicket.STATUS_OPEN, SupportTicket.STATUS_PENDING, SupportTicket.STATUS_PENDING_SUPPORT,
            SupportTicket.STATUS_PENDING_CUSTOMER, SupportTicket.STATUS_ESCALATED,
            SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED,
        ):
            t = _make_ticket(status=status)
            with self.assertRaises(IllegalTransition):
                apply_transition(t, SupportTicket.STATUS_PENDING, actor=self.owner)

    def test_legacy_pending_behaves_like_pending_support(self):
        t_legacy = _make_ticket(status=SupportTicket.STATUS_PENDING)
        t_support = _make_ticket(status=SupportTicket.STATUS_PENDING_SUPPORT)
        self.assertEqual(
            legal_next_statuses(t_legacy, self.support),
            legal_next_statuses(t_support, self.support),
        )

    def test_resolved_at_set_entering_resolved(self):
        t = _make_ticket(status=SupportTicket.STATUS_OPEN)
        apply_transition(t, SupportTicket.STATUS_RESOLVED, actor=self.support)
        self.assertIsNotNone(t.resolved_at)

    def test_closed_at_set_entering_closed(self):
        t = _make_ticket(status=SupportTicket.STATUS_RESOLVED)
        apply_transition(t, SupportTicket.STATUS_CLOSED, actor=self.support)
        self.assertIsNotNone(t.closed_at)

    def test_reopen_clears_closed_at_preserves_resolved_at(self):
        t = _make_ticket(status=SupportTicket.STATUS_RESOLVED)
        apply_transition(t, SupportTicket.STATUS_CLOSED, actor=self.support)
        old_resolved_at = t.resolved_at
        apply_transition(t, SupportTicket.STATUS_OPEN, actor=self.support)
        self.assertIsNone(t.closed_at)
        self.assertEqual(t.resolved_at, old_resolved_at)

    def test_illegal_pair_raises_illegal_transition(self):
        # CUSTOMER-SUPPORT-01C added a client-only OPEN -> CLOSED row,
        # so OPEN -> CLOSED is no longer illegal for every actor (it's
        # legal for the ticket's own client). RESOLVED -> ESCALATED has
        # no matching row for ANY actor in either block, so it remains
        # a genuine structural illegality.
        t = _make_ticket(status=SupportTicket.STATUS_RESOLVED, resolved_at=timezone.now())
        with self.assertRaises(IllegalTransition):
            apply_transition(t, SupportTicket.STATUS_ESCALATED, actor=self.owner)


# ── Escalation ───────────────────────────────────────────────────────────

class EscalationTests(TestCase):
    def setUp(self):
        self.support = _make_support()
        self.ticket = _make_ticket(status=SupportTicket.STATUS_OPEN)
        self.client.force_login(self.support)

    def test_reason_required(self):
        resp = self.client.post(_escalate_url(self.ticket), {"reason": ""})
        self.assertEqual(resp.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertNotEqual(self.ticket.status, SupportTicket.STATUS_ESCALATED)

    def test_fields_set_atomically(self):
        self.client.post(_escalate_url(self.ticket), {"reason": "fraud suspected"})
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.escalated_to_ops)
        self.assertIsNotNone(self.ticket.escalated_at)
        self.assertEqual(self.ticket.escalated_by_id, self.support.pk)
        self.assertEqual(self.ticket.escalation_reason, "fraud suspected")
        self.assertEqual(self.ticket.status, SupportTicket.STATUS_ESCALATED)

    def test_support_gains_no_ops_power_after_escalating(self):
        self.client.post(_escalate_url(self.ticket), {"reason": "fraud suspected"})
        self.ticket.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            apply_transition(self.ticket, SupportTicket.STATUS_RESOLVED, actor=self.support)

    def test_escalated_to_ops_remains_true_after_resolution(self):
        self.client.post(_escalate_url(self.ticket), {"reason": "fraud suspected"})
        self.ticket.refresh_from_db()
        owner = _make_owner(username="owner_esc")
        apply_transition(self.ticket, SupportTicket.STATUS_RESOLVED, actor=owner)
        self.assertTrue(self.ticket.escalated_to_ops)


# ── Thread visibility ────────────────────────────────────────────────────

class ThreadVisibilityTests(TestCase):
    def setUp(self):
        self.support = _make_support()
        self.ops_owner = _make_owner(username="thread_owner")
        self.ticket = _make_ticket()
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="customer visible reply", visibility=SupportMessage.VISIBILITY_CUSTOMER,
        )
        SupportMessage.objects.create(
            ticket=self.ticket, author=self.support, author_role=SupportMessage.ROLE_SUPPORT,
            body="internal only note", visibility=SupportMessage.VISIBILITY_INTERNAL,
        )

    def test_support_sees_both_visibilities_in_detail(self):
        self.client.force_login(self.support)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "customer visible reply")
        self.assertContains(resp, "internal only note")

    def test_owner_sees_both_visibilities_in_detail(self):
        self.client.force_login(self.ops_owner)
        resp = self.client.get(_detail_url(self.ticket))
        self.assertContains(resp, "customer visible reply")
        self.assertContains(resp, "internal only note")

    def test_author_role_snapshot_correct(self):
        msg = SupportMessage.objects.filter(visibility=SupportMessage.VISIBILITY_INTERNAL).first()
        self.assertEqual(msg.author_role, "SUPPORT")


# ── First response ───────────────────────────────────────────────────────

class FirstResponseTests(TestCase):
    def setUp(self):
        self.support = _make_support()
        self.ticket = _make_ticket()
        self.client.force_login(self.support)

    def test_set_once_on_first_reply(self):
        self.assertIsNone(self.ticket.first_response_at)
        self.client.post(_reply_url(self.ticket), {"body": "first reply"})
        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.first_response_at)

    def test_never_overwritten_by_later_reply(self):
        self.client.post(_reply_url(self.ticket), {"body": "first reply"})
        self.ticket.refresh_from_db()
        first_ts = self.ticket.first_response_at

        self.ticket.status = SupportTicket.STATUS_PENDING_SUPPORT
        self.ticket.save(update_fields=["status"])
        self.client.post(_reply_url(self.ticket), {"body": "second reply"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.first_response_at, first_ts)


# ── Reply-triggered status transition ───────────────────────────────────

class ReplyStatusTransitionTests(TestCase):
    def setUp(self):
        self.support = _make_support()
        self.client.force_login(self.support)

    def test_open_to_pending_customer_after_reply(self):
        t = _make_ticket(status=SupportTicket.STATUS_OPEN)
        self.client.post(_reply_url(t), {"body": "reply"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_PENDING_CUSTOMER)

    def test_escalated_status_unchanged_by_reply(self):
        t = _make_ticket(status=SupportTicket.STATUS_ESCALATED)
        self.client.post(_reply_url(t), {"body": "reply on escalated ticket"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_ESCALATED)

    def test_resolved_status_unchanged_by_reply(self):
        t = _make_ticket(status=SupportTicket.STATUS_RESOLVED, resolved_at=timezone.now())
        self.client.post(_reply_url(t), {"body": "reply on resolved ticket"})
        t.refresh_from_db()
        self.assertEqual(t.status, SupportTicket.STATUS_RESOLVED)
