# simulator/tests/test_o6c1g_account_product_features_admin_fix.py
"""
O.6c-1g — AccountProduct.features Admin fix.

Root cause (O.6c-1f, demonstrated): AccountProduct.features is a
JSONField(default=dict) with blank=False. Django's own
forms.Field.empty_values = (None, '', [], (), {}) treats an empty dict as
"nothing entered" — so a required (blank=False) JSONField whose
legitimate value IS {} gets rejected by Django Admin as "This field is
required" every time the form is submitted, even completely unchanged.
This has existed since the field's original creation (migration 0032),
unrelated to O.6c-1e's margin-policy fields (confirmed clean in O.6c-1f).

Fix: features = models.JSONField(default=dict, blank=True). null stays
False; default=dict is untouched; no other field is touched.

Tests:
  1. features={} is accepted by the ModelForm/Admin form.
  2. Editing the real Real Standard (id=3) fixture unchanged now validates.
  3. Creating a new AccountProduct with features={} validates.
  4. Non-empty JSON still works (no regression).
  5. Invalid JSON is still correctly rejected (blank=True must not make
     the field silently accept garbage).
"""
from decimal import Decimal

from django.contrib import admin as dj_admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from simulator.admin import AccountProductAdmin
from simulator.models import AccountProduct
from .factories import make_account_product

User = get_user_model()


def _admin_form_class(obj=None):
    rf = RequestFactory()
    req = rf.get("/x")
    req.user = User(is_superuser=True, is_staff=True)
    ma = AccountProductAdmin(AccountProduct, dj_admin.site)
    return ma.get_form(req, obj=obj)


def _full_data_for(obj, **overrides):
    """Every field's own current/initial value, as the real Admin form
    would render it — the same reconstruction technique used in the
    O.6c-1f diagnostic to reproduce an unchanged resubmission."""
    FormClass = _admin_form_class(obj)
    unbound = FormClass(instance=obj)
    data = {}
    for name in unbound.fields:
        v = unbound[name].value()
        data[name] = "" if v is None else v
    data.update(overrides)
    return data, FormClass


class FeaturesFieldMetaTests(TestCase):
    def test_blank_true_null_false_default_dict(self):
        f = AccountProduct._meta.get_field("features")
        self.assertTrue(f.blank)
        self.assertFalse(f.null)
        self.assertEqual(f.default, dict)


# ─────────────────────────────────────────────────────────────────────────
# 1. features={} accepted by the ModelForm
# ─────────────────────────────────────────────────────────────────────────
class EmptyDictAcceptedTests(TestCase):
    def test_features_empty_dict_string_valid(self):
        FormClass = _admin_form_class(obj=None)
        p = make_account_product(code="o6c1g-tmp")
        data, _ = _full_data_for(p)
        data["features"] = "{}"
        bound = FormClass(data=data, instance=AccountProduct.objects.get(pk=p.pk))
        self.assertTrue(bound.is_valid(), bound.errors)


# ─────────────────────────────────────────────────────────────────────────
# 2. Editing Real Standard-equivalent fixture unchanged now validates
# ─────────────────────────────────────────────────────────────────────────
class ExistingProductUnchangedResubmissionTests(TestCase):
    def test_unchanged_resubmission_of_existing_product_validates(self):
        """Reproduces the exact O.6c-1f scenario: a pre-existing product
        with features={} (the real historical state of every seeded
        AccountProduct, including id=3 "Real Standard"), resubmitted via
        Admin with every field's own current value, untouched."""
        obj = make_account_product(
            code="o6c1g-real-standard-like", product_type=AccountProduct.TYPE_STANDARD,
            family=AccountProduct.FAMILY_REAL,
        )
        self.assertEqual(obj.features, {})  # matches every real seeded product
        data, FormClass = _full_data_for(obj)
        self.assertEqual(data["features"], "{}")  # exactly what the widget renders
        bound = FormClass(data=data, instance=AccountProduct.objects.get(pk=obj.pk))
        self.assertTrue(bound.is_valid(), bound.errors)


# ─────────────────────────────────────────────────────────────────────────
# 3. Creating a new AccountProduct with features={} validates
# ─────────────────────────────────────────────────────────────────────────
class NewProductCreationTests(TestCase):
    def test_new_product_with_empty_features_validates(self):
        FormClass = _admin_form_class(obj=None)
        data = {
            "name": "New Test Product", "product_type": AccountProduct.TYPE_STANDARD,
            "family": AccountProduct.FAMILY_REAL, "platform_label": "Money Broker",
            "min_deposit": "100.00", "default_balance": "0.00", "max_leverage": "100",
            "typical_spread_pips": "0.00", "commission_per_lot": "0.00",
            "commission_pct": "0.0000", "spread_markup": "0.0000",
            "margin_call_level": "100.00", "stopout_level": "50.00",
            "max_margin_per_trade_pct": "10.00", "max_total_margin_pct": "50.00",
            "sort_order": "0", "features": "{}",
        }
        bound = FormClass(data=data)
        self.assertTrue(bound.is_valid(), bound.errors)


# ─────────────────────────────────────────────────────────────────────────
# 4/5. Non-empty JSON still works; invalid JSON still rejected
# ─────────────────────────────────────────────────────────────────────────
class NonEmptyAndInvalidJSONTests(TestCase):
    def test_non_empty_json_still_valid(self):
        obj = make_account_product(code="o6c1g-nonempty")
        data, FormClass = _full_data_for(obj, features='{"popular_badge": true}')
        bound = FormClass(data=data, instance=AccountProduct.objects.get(pk=obj.pk))
        self.assertTrue(bound.is_valid(), bound.errors)
        bound.full_clean()
        self.assertEqual(bound.cleaned_data["features"], {"popular_badge": True})

    def test_invalid_json_still_rejected(self):
        obj = make_account_product(code="o6c1g-invalid")
        data, FormClass = _full_data_for(obj, features="{not valid json")
        bound = FormClass(data=data, instance=AccountProduct.objects.get(pk=obj.pk))
        self.assertFalse(bound.is_valid())
        self.assertIn("features", bound.errors)

    def test_whitespace_only_still_rejected(self):
        """blank=True must not make garbage silently pass — whitespace is
        non-empty per Django's empty_values check but still fails json.loads."""
        obj = make_account_product(code="o6c1g-whitespace")
        data, FormClass = _full_data_for(obj, features="   ")
        bound = FormClass(data=data, instance=AccountProduct.objects.get(pk=obj.pk))
        self.assertFalse(bound.is_valid())
        self.assertIn("features", bound.errors)
