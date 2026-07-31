# simulator/tests/test_o2g1_treasury_operation_request.py
"""
Bloque O.2g-1 — Treasury Private Operations, infraestructura mínima.

Covers ONLY what this block builds: the TreasuryOperationRequest schema
(choices, constraint, indexes, Meta.permissions materialized via Django's
own post_migrate signal) and its 100%-read-only ModelAdmin.

No test here exercises request creation via a form, approval, rejection,
execution, credit_wallet()/debit_wallet(), or Treasury Hold — none of
that exists in the codebase yet, by design. The frozen state machine
(PENDING/APPROVED/REJECTED/EXECUTING/EXECUTED/FAILED/CANCELLED) is
asserted here only as *schema* (the choices exist, in the right values)
— no code enforces the transitions yet, so no test claims otherwise.
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth.models import Permission
from django.db import IntegrityError, models as db_models, transaction
from django.test import Client, TestCase
from django.urls import reverse

from simulator.admin import TreasuryOperationRequestAdmin
from simulator.models import TreasuryOperationRequest, Wallet, WalletTransaction

from .factories import make_user, make_wallet


class TreasuryOperationRequestModelTests(TestCase):

    def test_creates_with_minimal_fields(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
            wallet=wallet,
            amount=Decimal("50.00"),
        )
        self.assertIsNotNone(req.pk)

    def test_default_status_is_pending(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
            wallet=wallet,
            amount=Decimal("10.00"),
        )
        self.assertEqual(req.status, TreasuryOperationRequest.ST_PENDING)

    def test_operation_type_choices_match_frozen_design(self):
        values = {c[0] for c in TreasuryOperationRequest.OPERATION_TYPE_CHOICES}
        self.assertEqual(values, {
            "CREDIT_FUNDS", "DEBIT_FUNDS", "REFUND",
            "BONUS_CREDIT", "IB_COMMISSION", "MANUAL_ADJUSTMENT",
        })

    def test_treasury_hold_is_not_an_operation_type(self):
        # Explicitly excluded per the frozen architecture — Hold gets its
        # own model when it is separately authorized.
        values = {c[0] for c in TreasuryOperationRequest.OPERATION_TYPE_CHOICES}
        self.assertNotIn("TREASURY_HOLD", values)
        self.assertNotIn("HOLD", values)

    def test_status_choices_match_frozen_state_machine(self):
        values = {c[0] for c in TreasuryOperationRequest.STATUS_CHOICES}
        self.assertEqual(values, {
            "PENDING", "APPROVED", "REJECTED",
            "EXECUTING", "EXECUTED", "FAILED", "CANCELLED",
        })

    def test_category_choices_match_frozen_design(self):
        values = {c[0] for c in TreasuryOperationRequest.CATEGORY_CHOICES}
        self.assertEqual(values, {
            "SYSTEM_ERROR", "PROVIDER_DUPLICATE",
            "INCIDENT_COMPENSATION", "OTHER",
        })

    def test_amount_zero_violates_check_constraint(self):
        wallet = make_wallet()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreasuryOperationRequest.objects.create(
                    operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
                    wallet=wallet, amount=Decimal("0.00"),
                )

    def test_amount_negative_violates_check_constraint(self):
        wallet = make_wallet()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreasuryOperationRequest.objects.create(
                    operation_type=TreasuryOperationRequest.OP_DEBIT_FUNDS,
                    wallet=wallet, amount=Decimal("-5.00"),
                )

    def test_wallet_on_delete_is_protect(self):
        field = TreasuryOperationRequest._meta.get_field("wallet")
        self.assertIs(field.remote_field.on_delete, db_models.PROTECT)

    def test_wallet_transaction_is_one_to_one(self):
        field = TreasuryOperationRequest._meta.get_field("wallet_transaction")
        self.assertEqual(field.get_internal_type(), "OneToOneField")

    def test_wallet_transaction_uniqueness_enforced_at_db_level(self):
        wallet = make_wallet(initial_balance=Decimal("20"))
        wtx = WalletTransaction.objects.filter(wallet=wallet).first()
        self.assertIsNotNone(wtx)

        TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
            wallet=wallet, amount=Decimal("5.00"), wallet_transaction=wtx,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreasuryOperationRequest.objects.create(
                    operation_type=TreasuryOperationRequest.OP_DEBIT_FUNDS,
                    wallet=wallet, amount=Decimal("7.00"), wallet_transaction=wtx,
                )

    def test_requested_by_survives_user_deletion(self):
        user = make_user(username="o2g1_req_owner")
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_REFUND,
            wallet=wallet, amount=Decimal("15.00"), requested_by=user,
        )
        user.delete()
        req.refresh_from_db()
        self.assertIsNone(req.requested_by)
        self.assertIsNotNone(req.pk)  # the request row itself survives

    def test_ordering_is_newest_first(self):
        wallet = make_wallet()
        first = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
            wallet=wallet, amount=Decimal("1.00"),
        )
        second = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
            wallet=wallet, amount=Decimal("2.00"),
        )
        ordered = list(TreasuryOperationRequest.objects.all())
        self.assertEqual(ordered[0].pk, second.pk)
        self.assertEqual(ordered[1].pk, first.pk)


class CancelledAtFieldTests(TestCase):
    """
    O.2g-1a — CANCELLED state audit completion. Only cancelled_at was
    authorized; cancelled_by was explicitly evaluated and rejected (the
    actor is deferred to AuditLog/BrokerAuditEvent, same discipline
    AUDIT-03 already uses for KYCProfile). No cancellation service exists
    yet, so nothing here sets cancelled_at automatically — these tests
    only prove the field's schema, not any transition behavior.
    """

    def test_cancelled_at_field_exists(self):
        field = TreasuryOperationRequest._meta.get_field("cancelled_at")
        self.assertEqual(field.get_internal_type(), "DateTimeField")

    def test_cancelled_at_accepts_null(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_CREDIT_FUNDS,
            wallet=wallet, amount=Decimal("40.00"),
        )
        self.assertIsNone(req.cancelled_at)

    def test_cancelled_at_accepts_a_real_datetime(self):
        from django.utils import timezone

        wallet = make_wallet()
        now = timezone.now()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_REFUND,
            wallet=wallet, amount=Decimal("25.00"), cancelled_at=now,
        )
        req.refresh_from_db()
        self.assertEqual(req.cancelled_at, now)

    def test_creating_a_request_never_sets_cancelled_at_automatically(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
            wallet=wallet, amount=Decimal("9.00"),
        )
        self.assertIsNone(req.cancelled_at)
        self.assertEqual(req.status, TreasuryOperationRequest.ST_PENDING)

    def test_setting_cancelled_at_does_not_change_status_by_itself(self):
        # Schema allows it, but no service exists that would ever do this —
        # proves there is no hidden signal/save() side effect linking the two.
        from django.utils import timezone

        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_DEBIT_FUNDS,
            wallet=wallet, amount=Decimal("3.00"), cancelled_at=timezone.now(),
        )
        self.assertEqual(req.status, TreasuryOperationRequest.ST_PENDING)

    def test_cancelled_status_still_present_in_choices(self):
        values = {c[0] for c in TreasuryOperationRequest.STATUS_CHOICES}
        self.assertIn("CANCELLED", values)

    def test_cancelled_by_field_was_not_added(self):
        field_names = {f.name for f in TreasuryOperationRequest._meta.get_fields()}
        self.assertNotIn("cancelled_by", field_names)

    def test_cancelled_at_is_readonly_in_admin(self):
        from django.contrib.admin.sites import site as _site
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, _site)
        self.assertIn("cancelled_at", ma.readonly_fields)

    def test_admin_still_has_no_add_change_delete_permission(self):
        from django.contrib.admin.sites import site as _site
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, _site)
        self.assertFalse(ma.has_add_permission(request=None))
        self.assertFalse(ma.has_change_permission(request=None))
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_cancelled_at_visible_on_admin_change_view(self):
        from django.utils import timezone

        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
            wallet=wallet, amount=Decimal("18.00"), cancelled_at=timezone.now(),
        )
        staff = make_user(username="o2g1a_admin_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(
            reverse("admin:simulator_treasuryoperationrequest_change", args=[req.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cancelled at")
        self.assertNotIn(b'name="_save"', resp.content)


class TreasuryOperationRequestPermissionsMaterializedTests(TestCase):
    """
    Verifies the Fase 1 technical claim: Meta.permissions materialize via
    Django's own post_migrate signal, with no data migration involved.
    """

    def test_can_review_permission_exists(self):
        self.assertTrue(
            Permission.objects.filter(
                content_type__app_label="simulator",
                content_type__model="treasuryoperationrequest",
                codename="can_review_treasury_request",
            ).exists()
        )

    def test_can_execute_permission_exists(self):
        self.assertTrue(
            Permission.objects.filter(
                content_type__app_label="simulator",
                content_type__model="treasuryoperationrequest",
                codename="can_execute_treasury_request",
            ).exists()
        )

    def test_default_crud_permissions_also_exist(self):
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="simulator",
                content_type__model="treasuryoperationrequest",
            ).values_list("codename", flat=True)
        )
        self.assertTrue({"add_treasuryoperationrequest",
                          "change_treasuryoperationrequest",
                          "delete_treasuryoperationrequest",
                          "view_treasuryoperationrequest"}.issubset(codenames))


class TreasuryOperationRequestAdminPermissionTests(TestCase):

    def test_registered(self):
        self.assertIn(TreasuryOperationRequest, admin_site._registry)

    def test_cannot_add(self):
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_all_fields_readonly(self):
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, admin_site)
        model_fields = {f.name for f in TreasuryOperationRequest._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_delete_selected_not_available(self):
        from django.test import RequestFactory
        ma = TreasuryOperationRequestAdmin(TreasuryOperationRequest, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="o2g1_admin_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)
        # No custom action exists yet either — this block adds no actions.
        self.assertEqual(actions, {})

    def test_changelist_loads_without_error(self):
        wallet = make_wallet()
        TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
            wallet=wallet, amount=Decimal("30.00"),
        )
        staff = make_user(username="o2g1_admin_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_treasuryoperationrequest_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected(self):
        staff = make_user(username="o2g1_admin_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(reverse("admin:simulator_treasuryoperationrequest_add"))
        self.assertEqual(resp.status_code, 403)

    def test_delete_view_rejected(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_IB_COMMISSION,
            wallet=wallet, amount=Decimal("8.00"),
        )
        staff = make_user(username="o2g1_admin_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(
            reverse("admin:simulator_treasuryoperationrequest_delete", args=[req.pk])
        )
        self.assertEqual(resp.status_code, 403)

    def test_change_view_read_only(self):
        wallet = make_wallet()
        req = TreasuryOperationRequest.objects.create(
            operation_type=TreasuryOperationRequest.OP_REFUND,
            wallet=wallet, amount=Decimal("12.00"),
        )
        staff = make_user(username="o2g1_admin_staff5", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        resp = client.get(
            reverse("admin:simulator_treasuryoperationrequest_change", args=[req.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'name="_save"', resp.content)
