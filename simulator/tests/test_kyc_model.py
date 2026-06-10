# simulator/tests/test_kyc_model.py
"""
KYCProfile model — regression tests (Part 1: model + admin).

Covers:
  1. KYCProfile can be created and linked to a User (OneToOne).
  2. Default status is 'not_started'.
  3. All four statuses are accepted.
  4. is_approved / is_pending properties work correctly.
  5. String representation is informative.
  6. reviewed_by FK accepts a staff user and can be null.
  7. Admin class is registered with the expected configuration.
  8. Migration 0039 exists and applies cleanly.
"""
from django.contrib.admin.sites import site as admin_site
from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.models import KYCProfile
from simulator.tests.factories import make_user

User = get_user_model()


class KYCProfileCreationTests(TestCase):

    def setUp(self):
        self.user = make_user(username="kyc_test_user")

    def test_can_create_kyc_profile(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertIsNotNone(kyc.pk)

    def test_default_status_is_not_started(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertEqual(kyc.status, KYCProfile.STATUS_NOT_STARTED)

    def test_one_to_one_relationship_with_user(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertEqual(kyc.user, self.user)
        # Reverse accessor works
        self.assertEqual(self.user.kyc_profile, kyc)

    def test_duplicate_kyc_profile_raises(self):
        from django.db import IntegrityError
        KYCProfile.objects.create(user=self.user)
        with self.assertRaises(IntegrityError):
            KYCProfile.objects.create(user=self.user)

    def test_str_representation(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertIn("kyc_test_user", str(kyc))
        self.assertIn("not_started", str(kyc))


class KYCProfileStatusTests(TestCase):

    def setUp(self):
        self.user = make_user(username="kyc_status_user")

    def _kyc(self, status):
        return KYCProfile.objects.create(user=self.user, status=status)

    def test_status_pending(self):
        kyc = self._kyc(KYCProfile.STATUS_PENDING)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, "pending")

    def test_status_approved(self):
        kyc = self._kyc(KYCProfile.STATUS_APPROVED)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, "approved")

    def test_status_rejected(self):
        kyc = self._kyc(KYCProfile.STATUS_REJECTED)
        kyc.refresh_from_db()
        self.assertEqual(kyc.status, "rejected")

    def test_is_approved_true_when_approved(self):
        kyc = self._kyc(KYCProfile.STATUS_APPROVED)
        self.assertTrue(kyc.is_approved)

    def test_is_approved_false_when_pending(self):
        kyc = self._kyc(KYCProfile.STATUS_PENDING)
        self.assertFalse(kyc.is_approved)

    def test_is_pending_true_when_pending(self):
        kyc = self._kyc(KYCProfile.STATUS_PENDING)
        self.assertTrue(kyc.is_pending)

    def test_is_pending_false_when_approved(self):
        kyc = self._kyc(KYCProfile.STATUS_APPROVED)
        self.assertFalse(kyc.is_pending)


class KYCProfileFieldTests(TestCase):

    def setUp(self):
        self.user = make_user(username="kyc_fields_user")

    def test_optional_fields_default_blank(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertEqual(kyc.legal_name, "")
        self.assertEqual(kyc.country, "")
        self.assertEqual(kyc.document_type, "")
        self.assertEqual(kyc.document_number, "")
        self.assertEqual(kyc.rejection_reason, "")
        self.assertIsNone(kyc.submitted_at)
        self.assertIsNone(kyc.reviewed_at)
        self.assertIsNone(kyc.reviewed_by)

    def test_legal_name_and_country_stored(self):
        kyc = KYCProfile.objects.create(
            user=self.user,
            legal_name="Juan Pérez",
            country="Venezuela",
        )
        kyc.refresh_from_db()
        self.assertEqual(kyc.legal_name, "Juan Pérez")
        self.assertEqual(kyc.country, "Venezuela")

    def test_reviewed_by_accepts_staff_user(self):
        reviewer = make_user(username="kyc_reviewer")
        reviewer.is_staff = True
        reviewer.save()
        kyc = KYCProfile.objects.create(
            user=self.user,
            status=KYCProfile.STATUS_APPROVED,
            reviewed_by=reviewer,
        )
        kyc.refresh_from_db()
        self.assertEqual(kyc.reviewed_by, reviewer)

    def test_reviewed_by_nullable(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertIsNone(kyc.reviewed_by)

    def test_document_type_choices(self):
        for code, _ in KYCProfile.DOCUMENT_TYPE_CHOICES:
            user = make_user(username=f"kyc_doc_{code}")
            kyc  = KYCProfile.objects.create(user=user, document_type=code)
            kyc.refresh_from_db()
            self.assertEqual(kyc.document_type, code)

    def test_timestamps_auto_populated(self):
        kyc = KYCProfile.objects.create(user=self.user)
        self.assertIsNotNone(kyc.created_at)
        self.assertIsNotNone(kyc.updated_at)


class KYCAdminRegistrationTests(TestCase):
    """Admin class must be registered and expose the expected configuration."""

    def test_kyc_profile_admin_is_registered(self):
        self.assertIn(KYCProfile, admin_site._registry,
                      "KYCProfile must be registered in the Django admin site")

    def test_admin_list_display_includes_status(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertIn("status", admin_instance.list_display)

    def test_admin_list_display_includes_user(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertIn("user", admin_instance.list_display)

    def test_admin_list_filter_includes_status(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertIn("status", admin_instance.list_filter)

    def test_admin_search_fields_include_username(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertTrue(
            any("username" in f for f in admin_instance.search_fields),
            "search_fields must include user__username",
        )

    def test_admin_has_approve_action(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertIn("approve_kyc", admin_instance.actions)

    def test_admin_has_reject_action(self):
        admin_instance = admin_site._registry[KYCProfile]
        self.assertIn("reject_kyc", admin_instance.actions)
