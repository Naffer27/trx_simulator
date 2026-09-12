# simulator/tests/test_useradmin_hierarchy.py
"""
MONEY-INTEGRITY-FIX-02 — TreasuryHardenedUserAdmin permission-hierarchy
extension (section 15 of the implementation authorization).

test_owner_root.py already covers the Owner-target full-form-lock from a
plain "other superuser" actor's perspective. This file adds the
remaining hierarchy angles: an actual OPS_ADMIN actor (not just any
superuser), the is_customer_support queryset exclusion introduced by
this block, is_staff protection, and confirms Owner Root itself is
never blocked from managing a lower-role target.
"""
from django.contrib.auth.models import Permission
from django.contrib.admin.sites import AdminSite
from django.contrib.contenttypes.models import ContentType
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.admin import TreasuryHardenedUserAdmin
from simulator.models import OpsAdminProfile, OwnerRoot
from .factories import make_user


def _make_owner(**kwargs):
    user = make_user(is_superuser=True, is_staff=True, **kwargs)
    OwnerRoot.objects.create(user=user, established_by="test")
    return user


def _make_ops(owner, **kwargs):
    user = make_user(is_staff=True, **kwargs)
    OpsAdminProfile.objects.create(user=user, assigned_by=owner)
    return user


def _support_permission():
    return Permission.objects.get(
        content_type__app_label="simulator", codename="is_customer_support",
    )


class UserAdminHierarchyTests(TestCase):
    def setUp(self):
        self.owner = _make_owner(username="owner_h")
        self.ops = _make_ops(self.owner, username="ops_h")
        self.ma = TreasuryHardenedUserAdmin(self.owner.__class__, AdminSite())

    def _request(self, actor):
        req = RequestFactory().get("/admin/auth/user/1/change/")
        req.user = actor
        return req

    # -- Ops cannot modify the Owner target (full-form lock) -------------

    def test_ops_cannot_modify_owner_target(self):
        request = self._request(self.ops)
        form_class = self.ma.get_form(request, obj=self.owner)
        form = form_class(instance=self.owner)
        for name, field in form.fields.items():
            self.assertTrue(field.disabled, f"field {name!r} should be disabled for an OPS editor")

    # -- Ops cannot grant is_staff -----------------------------------------

    def test_ops_cannot_change_is_staff_on_lower_target(self):
        lower = make_user(username="lower_h")
        request = self._request(self.ops)
        form_class = self.ma.get_form(request, obj=lower)
        form = form_class(instance=lower)
        self.assertTrue(form.fields["is_staff"].disabled)

    def test_ops_cannot_change_is_staff_on_self(self):
        request = self._request(self.ops)
        form_class = self.ma.get_form(request, obj=self.ops)
        form = form_class(instance=self.ops)
        self.assertTrue(form.fields["is_staff"].disabled)

    # -- Owner IS allowed to change is_staff / manage lower roles ---------

    def test_owner_can_change_is_staff_on_lower_target(self):
        lower = make_user(username="lower_h2")
        request = self._request(self.owner)
        form_class = self.ma.get_form(request, obj=lower)
        form = form_class(instance=lower)
        self.assertFalse(form.fields["is_staff"].disabled)

    def test_owner_not_blocked_editing_ops_target(self):
        request = self._request(self.owner)
        form_class = self.ma.get_form(request, obj=self.ops)
        form = form_class(instance=self.ops)
        # Owner target full-lock only triggers for the OwnerRoot user —
        # editing the OPS_ADMIN target must not be blanket-disabled.
        self.assertFalse(form.fields["first_name"].disabled)

    # -- is_customer_support queryset exclusion ---------------------------

    def test_non_ops_actor_cannot_see_support_permission_option(self):
        plain_superuser = make_user(is_superuser=True, is_staff=True, username="plain_su")
        lower = make_user(username="lower_h3")
        request = self._request(plain_superuser)
        self.ma.get_form(request, obj=lower)  # populates request._o4b2_target_user
        field = self.ma.formfield_for_manytomany(
            self.owner.__class__.user_permissions.field, request,
        )
        self.assertNotIn(_support_permission().pk, field.queryset.values_list("pk", flat=True))

    def test_ops_actor_can_see_support_permission_option(self):
        lower = make_user(username="lower_h4")
        request = self._request(self.ops)
        request.user.is_superuser = True  # bypass the unrelated Treasury-permission exclusion branch
        self.ma.get_form(request, obj=lower)
        field = self.ma.formfield_for_manytomany(
            self.owner.__class__.user_permissions.field, request,
        )
        self.assertIn(_support_permission().pk, field.queryset.values_list("pk", flat=True))

    def test_owner_actor_can_see_support_permission_option(self):
        lower = make_user(username="lower_h5")
        request = self._request(self.owner)
        self.ma.get_form(request, obj=lower)
        field = self.ma.formfield_for_manytomany(
            self.owner.__class__.user_permissions.field, request,
        )
        self.assertIn(_support_permission().pk, field.queryset.values_list("pk", flat=True))

    def test_second_superuser_cannot_grant_support_permission(self):
        # A superuser who is NOT Owner Root and NOT OPS_ADMIN must not be
        # treated as eligible to grant is_customer_support either.
        second_su = make_user(is_superuser=True, is_staff=True, username="second_su")
        lower = make_user(username="lower_h6")
        request = self._request(second_su)
        self.ma.get_form(request, obj=lower)
        field = self.ma.formfield_for_manytomany(
            self.owner.__class__.user_permissions.field, request,
        )
        self.assertNotIn(_support_permission().pk, field.queryset.values_list("pk", flat=True))


# ─────────────────────────────────────────────────────────────────────
# MONEY-INTEGRITY-FIX-02 hardening patch — save_related() preservation
# of is_customer_support for restricted actors (real HTTP admin path,
# same shape/helpers as test_o4b2/test_o4b3/test_o4b4's own end-to-end
# Treasury-permission-preservation tests).
# ─────────────────────────────────────────────────────────────────────

def _user_content_type_permission(codename):
    from django.contrib.auth.models import User
    ct = ContentType.objects.get_for_model(User)
    return Permission.objects.get(content_type=ct, codename=codename)


def _grant_user_admin_access(user):
    user.user_permissions.add(
        _user_content_type_permission("view_user"),
        _user_content_type_permission("change_user"),
    )
    user.refresh_from_db()
    return user


def _user_change_url(user):
    return reverse("admin:auth_user_change", args=[user.pk])


def _change_payload(target, *, is_staff=None, is_superuser=None, user_permission_pks=None,
                     **overrides):
    if is_staff is None:
        is_staff = target.is_staff
    if is_superuser is None:
        is_superuser = target.is_superuser
    if user_permission_pks is None:
        user_permission_pks = list(target.user_permissions.values_list("pk", flat=True))

    data = {
        "username": target.username,
        "first_name": target.first_name or "",
        "last_name": target.last_name or "",
        "email": target.email or "",
        "date_joined_0": target.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": target.date_joined.strftime("%H:%M:%S"),
        "user_permissions": [str(pk) for pk in user_permission_pks],
    }
    if target.is_active:
        data["is_active"] = "on"
    if is_staff:
        data["is_staff"] = "on"
    if is_superuser:
        data["is_superuser"] = "on"
    data.update(overrides)
    return data


class SaveRelatedCustomerSupportPreservationTests(TestCase):
    """
    Root cause: formfield_for_manytomany() already excludes
    is_customer_support from a restricted actor's submittable queryset,
    but (before this patch) save_related() never restored a
    pre-existing grant that Django's user_permissions.set() call would
    otherwise silently drop. This class drives the real admin HTTP path
    (never internal helpers) to prove the fix end-to-end.
    """

    def setUp(self):
        self.owner = _make_owner(username="scs_owner")
        self.ops = _make_ops(self.owner, username="scs_ops")
        _grant_user_admin_access(self.ops)
        self.support_perm = _support_permission()
        self.client = Client()

    def test_restricted_actor_cannot_grant_customer_support(self):
        restricted = make_user(username="scs_restricted_grant", is_staff=True)
        _grant_user_admin_access(restricted)
        target = make_user(username="scs_target_grant", is_staff=True)
        self.assertFalse(target.user_permissions.filter(pk=self.support_perm.pk).exists())

        self.client.force_login(restricted)
        payload = _change_payload(
            target, user_permission_pks=[self.support_perm.pk],
        )
        resp = self.client.post(_user_change_url(target), data=payload)
        # is_customer_support is excluded from this actor's submittable
        # queryset entirely (formfield_for_manytomany), so a raw POST
        # containing its pk fails ModelMultipleChoiceField.clean() — the
        # form is re-rendered with an error, nothing is saved.
        self.assertEqual(resp.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.user_permissions.filter(pk=self.support_perm.pk).exists())

    def test_restricted_actor_cannot_remove_customer_support(self):
        restricted = make_user(username="scs_restricted_remove", is_staff=True)
        _grant_user_admin_access(restricted)
        target = make_user(username="scs_target_remove", is_staff=True)
        target.user_permissions.add(self.support_perm)

        self.client.force_login(restricted)
        payload = _change_payload(target, user_permission_pks=[])
        resp = self.client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertTrue(target.user_permissions.filter(pk=self.support_perm.pk).exists())

    def test_restricted_actor_editing_unrelated_field_preserves_customer_support(self):
        restricted = make_user(username="scs_restricted_unrelated", is_staff=True)
        _grant_user_admin_access(restricted)
        target = make_user(username="scs_target_unrelated", is_staff=True)
        target.user_permissions.add(self.support_perm)

        self.client.force_login(restricted)
        # A restricted actor's rendered form never lists is_customer_
        # support as a checkbox option at all (formfield_for_manytomany
        # excludes it from the queryset), so a real submission from this
        # actor would never include its pk regardless of the target's
        # current state — simulate that here.
        payload = _change_payload(target, first_name="Changed", user_permission_pks=[])
        resp = self.client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.first_name, "Changed")
        self.assertTrue(target.user_permissions.filter(pk=self.support_perm.pk).exists())

    def test_owner_can_grant_customer_support(self):
        target = make_user(username="scs_target_owner_grant", is_staff=True)
        self.client.force_login(self.owner)
        payload = _change_payload(target, user_permission_pks=[self.support_perm.pk])
        resp = self.client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertTrue(target.user_permissions.filter(pk=self.support_perm.pk).exists())

    def test_owner_can_remove_customer_support(self):
        target = make_user(username="scs_target_owner_remove", is_staff=True)
        target.user_permissions.add(self.support_perm)
        self.client.force_login(self.owner)
        payload = _change_payload(target, user_permission_pks=[])
        resp = self.client.post(_user_change_url(target), data=payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertFalse(target.user_permissions.filter(pk=self.support_perm.pk).exists())

    def test_ops_can_grant_and_remove_customer_support(self):
        # Per the approved Design Lock, OPS ADMIN administers
        # is_customer_support the same as Owner Root.
        target = make_user(username="scs_target_ops", is_staff=True)
        self.client.force_login(self.ops)

        grant_payload = _change_payload(target, user_permission_pks=[self.support_perm.pk])
        resp = self.client.post(_user_change_url(target), data=grant_payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertTrue(target.user_permissions.filter(pk=self.support_perm.pk).exists())

        remove_payload = _change_payload(target, user_permission_pks=[])
        resp = self.client.post(_user_change_url(target), data=remove_payload)
        self.assertEqual(resp.status_code, 302)
        target.refresh_from_db()
        self.assertFalse(target.user_permissions.filter(pk=self.support_perm.pk).exists())
