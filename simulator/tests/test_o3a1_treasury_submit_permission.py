# simulator/tests/test_o3a1_treasury_submit_permission.py
"""
Bloque O.3a-1 — Treasury Request Submission Engine, permiso base.

Covers ONLY the single change this block makes: declaring
"can_submit_treasury_request" in TreasuryOperationRequest.Meta.permissions
(simulator/models.py) and its materialization into auth_permission via
Django's own post_migrate signal, exactly like can_review_treasury_request
and can_execute_treasury_request were materialized in O.2g-1.

No view, form, service, URL, template or audit event exists yet for this
permission — nothing checks it, nothing grants it to any user or group.
These tests only prove the permission exists and is correctly scoped to
TreasuryOperationRequest; they assert nothing about how it will be used.
"""
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from simulator.models import TreasuryOperationRequest

from .factories import make_user


class TreasurySubmitPermissionExistsTests(TestCase):

    def test_permission_materialized_in_auth_permission(self):
        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        self.assertTrue(
            Permission.objects.filter(
                codename="can_submit_treasury_request",
                content_type=content_type,
            ).exists()
        )

    def test_permission_display_name(self):
        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        perm = Permission.objects.get(
            codename="can_submit_treasury_request", content_type=content_type,
        )
        self.assertEqual(perm.name, "Can submit (create) treasury request")

    def test_permission_scoped_to_treasuryoperationrequest_content_type(self):
        perm = Permission.objects.get(codename="can_submit_treasury_request")
        self.assertEqual(perm.content_type.model, "treasuryoperationrequest")
        self.assertEqual(perm.content_type.app_label, "simulator")

    def test_all_four_treasury_permissions_coexist(self):
        """
        O.3c-4c-1: TreasuryOperationRequest now carries four custom
        permissions — can_recover_treasury_execution was added and
        authorized in O.3c-4c. This guard originally hardcoded three;
        updated to reflect that approved addition, same "guard correctly
        falsified by the next authorized block" pattern already handled
        for O.3c-1 -> O.3c-3 (O.3c-3a) and O.3c-4a -> O.3c-4b (O.3c-4b-1).
        """
        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        codenames = set(
            Permission.objects.filter(content_type=content_type)
            .values_list("codename", flat=True)
        )
        self.assertEqual(
            codenames,
            {
                "add_treasuryoperationrequest",
                "change_treasuryoperationrequest",
                "delete_treasuryoperationrequest",
                "view_treasuryoperationrequest",
                "can_submit_treasury_request",
                "can_review_treasury_request",
                "can_execute_treasury_request",
                "can_recover_treasury_execution",
            },
        )

    def test_meta_permissions_declares_submit_first_of_four(self):
        # Schema-level check, independent of DB state: the tuple itself,
        # not what Django materialized from it. can_recover_treasury_
        # execution (O.3c-4c) appended last, submit still declared first.
        codenames = [c for c, _ in TreasuryOperationRequest._meta.permissions]
        self.assertEqual(
            codenames,
            [
                "can_submit_treasury_request",
                "can_review_treasury_request",
                "can_execute_treasury_request",
                "can_recover_treasury_execution",
            ],
        )


class TreasurySubmitPermissionNotYetUsedTests(TestCase):
    """
    O.3a-1 is schema-only. Confirms the permission grants nothing by
    itself yet — no view, admin action or service checks it. Only proves
    absence of use, not any future behavior.
    """

    def test_granting_permission_to_a_user_does_not_error(self):
        user = make_user(username="o3a1_grantee", is_staff=True)
        content_type = ContentType.objects.get_for_model(TreasuryOperationRequest)
        perm = Permission.objects.get(
            codename="can_submit_treasury_request", content_type=content_type,
        )
        user.user_permissions.add(perm)
        user.refresh_from_db()
        self.assertTrue(user.has_perm("simulator.can_submit_treasury_request"))

    def test_user_without_permission_does_not_have_it_by_default(self):
        user = make_user(username="o3a1_no_grant", is_staff=True)
        self.assertFalse(user.has_perm("simulator.can_submit_treasury_request"))
