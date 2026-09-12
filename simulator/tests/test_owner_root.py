# simulator/tests/test_owner_root.py
"""
MONEY-INTEGRITY-FIX-02 — OwnerRoot singleton + is_owner_root().

Covers:
  1. First OwnerRoot row is allowed.
  2. singleton_enforcer=False is rejected at the DB level (CheckConstraint).
  3. A second row with singleton_enforcer=True is rejected (UniqueConstraint).
  4. A second superuser does NOT become Owner (is_owner_root() is data-driven,
     never inferred from is_superuser).
  5. OwnerRootAdmin denies add/change/delete unconditionally, including
     for superusers.
  6. The Owner user's own admin form is fully locked for anyone but
     themselves (Owner target protection).
"""
from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, transaction
from django.test import TestCase

from simulator.admin import OwnerRootAdmin, TreasuryHardenedUserAdmin
from simulator.models import OwnerRoot
from simulator.permission_levels import is_owner_root
from .factories import make_user


class OwnerRootSingletonTests(TestCase):
    def test_first_ownerroot_allowed(self):
        user = make_user(is_superuser=True, is_staff=True)
        owner = OwnerRoot.objects.create(user=user, established_by="test")
        self.assertEqual(OwnerRoot.objects.count(), 1)
        self.assertTrue(is_owner_root(user))

    def test_false_singleton_enforcer_rejected(self):
        user = make_user()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OwnerRoot.objects.create(user=user, established_by="test", singleton_enforcer=False)

    def test_second_ownerroot_true_rejected(self):
        user1 = make_user()
        user2 = make_user()
        OwnerRoot.objects.create(user=user1, established_by="test")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OwnerRoot.objects.create(user=user2, established_by="test")
        self.assertEqual(OwnerRoot.objects.count(), 1)

    def test_second_superuser_not_owner(self):
        owner_user = make_user(is_superuser=True, is_staff=True)
        OwnerRoot.objects.create(user=owner_user, established_by="test")
        other_superuser = make_user(is_superuser=True, is_staff=True)
        self.assertFalse(is_owner_root(other_superuser))
        self.assertTrue(is_owner_root(owner_user))


class OwnerRootAdminLockedTests(TestCase):
    def setUp(self):
        self.owner_user = make_user(is_superuser=True, is_staff=True)
        OwnerRoot.objects.create(user=self.owner_user, established_by="test")
        self.ma = OwnerRootAdmin(OwnerRoot, AdminSite())

    def test_ownerroot_admin_add_false(self):
        self.assertFalse(self.ma.has_add_permission(request=None))

    def test_ownerroot_admin_change_false(self):
        self.assertFalse(self.ma.has_change_permission(request=None))

    def test_ownerroot_admin_delete_false(self):
        self.assertFalse(self.ma.has_delete_permission(request=None))


class OwnerTargetProtectionTests(TestCase):
    """
    TreasuryHardenedUserAdmin.get_form() must fully lock the form when the
    target IS the OwnerRoot user, for anyone editing who is not Owner
    Root themselves.
    """
    def setUp(self):
        self.owner_user = make_user(is_superuser=True, is_staff=True, username="owner_target")
        OwnerRoot.objects.create(user=self.owner_user, established_by="test")
        self.other_superuser = make_user(is_superuser=True, is_staff=True)
        self.ma = TreasuryHardenedUserAdmin(self.owner_user.__class__, AdminSite())

    def _fake_request(self, actor):
        from django.test import RequestFactory
        req = RequestFactory().get("/admin/auth/user/1/change/")
        req.user = actor
        return req

    def test_owner_user_protected_from_inferior_actor(self):
        request = self._fake_request(self.other_superuser)
        form_class = self.ma.get_form(request, obj=self.owner_user)
        form = form_class(instance=self.owner_user)
        for name, field in form.fields.items():
            self.assertTrue(field.disabled, f"field {name!r} should be disabled for a non-Owner editor")

    def test_owner_can_edit_own_account(self):
        request = self._fake_request(self.owner_user)
        form_class = self.ma.get_form(request, obj=self.owner_user)
        form = form_class(instance=self.owner_user)
        # At least one non-Owner-target-locked field remains editable —
        # is_staff/is_superuser still follow their own independent rules
        # (Owner editing self keeps is_superuser enabled since Owner IS a
        # superuser and is_staff enabled since actor_is_owner is True),
        # but the full-form lock from Owner-target-protection itself must
        # not apply when the editor IS the Owner.
        self.assertFalse(form.fields["first_name"].disabled)
