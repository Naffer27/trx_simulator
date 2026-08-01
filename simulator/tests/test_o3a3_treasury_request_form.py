# simulator/tests/test_o3a3_treasury_request_form.py
"""
Bloque O.3a-3 — TreasuryOperationRequestForm, formulario aislado.

Covers ONLY the single change this block makes: the isolated
TreasuryOperationRequestForm class in simulator/forms.py (plus its
WalletChoiceField helper and the evidence whitelist constants).

No view, URL, template, button, real AuditLog/BrokerAuditEvent row, or
productive TreasuryOperationRequest exists as a result of this block —
every test here calls form.is_valid()/cleaned_data/errors directly, or
(for the one save(commit=False) test) constructs an in-memory instance
without ever calling .save() with commit=True. TreasuryOperationRequest.
objects.count() is asserted to stay at 0 across every test in this file.
"""
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from simulator.forms import (
    TREASURY_EVIDENCE_ALLOWED_CONTENT_TYPES,
    TREASURY_EVIDENCE_ALLOWED_EXTENSIONS,
    TREASURY_EVIDENCE_MAX_SIZE_BYTES,
    TreasuryOperationRequestForm,
    WalletChoiceField,
)
from simulator.models import TreasuryOperationRequest, Wallet

from .factories import make_user, make_wallet


def _minimal_valid_data(wallet, **overrides):
    data = {
        "wallet": wallet.pk,
        "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
        "amount": "50.00",
        "reason": "Promo campaign credit",
    }
    data.update(overrides)
    return data


class ExposedFieldsTests(TestCase):
    """Campos expuestos exactos + campos sensibles ausentes."""

    def setUp(self):
        self.wallet = make_wallet()

    def test_exposed_fields_are_exactly_the_approved_eight(self):
        form = TreasuryOperationRequestForm()
        self.assertEqual(
            list(form.fields.keys()),
            ["wallet", "operation_type", "amount", "reason",
             "reference", "category", "comment", "evidence"],
        )

    def test_wallet_field_is_walletchoicefield(self):
        form = TreasuryOperationRequestForm()
        self.assertIsInstance(form.fields["wallet"], WalletChoiceField)

    def test_currency_field_not_exposed(self):
        form = TreasuryOperationRequestForm()
        self.assertNotIn("currency", form.fields)

    def test_sensitive_and_protected_fields_not_exposed(self):
        form = TreasuryOperationRequestForm()
        forbidden = [
            "status", "metadata", "wallet_transaction",
            "requested_by", "requested_at",
            "approved_by", "approved_at",
            "rejected_by", "rejected_at", "rejection_reason",
            "executed_by", "executed_at", "failure_reason",
            "cancelled_at", "updated_at",
        ]
        for field_name in forbidden:
            with self.subTest(field=field_name):
                self.assertNotIn(field_name, form.fields)

    def test_currency_cannot_be_injected_via_post_data(self):
        # Even if a malicious/mistaken client sends a "currency" key,
        # Meta.fields excludes it — cleaned_data never contains it.
        data = _minimal_valid_data(self.wallet, currency="EUR")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("currency", form.cleaned_data)


class WalletLookupTests(TestCase):
    """Wallet obligatorio, existente, y con label humano (username/email)."""

    def test_wallet_required(self):
        data = _minimal_valid_data(make_wallet())
        data["wallet"] = ""
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("wallet", form.errors)

    def test_nonexistent_wallet_id_is_rejected(self):
        wallet = make_wallet()
        bogus_id = wallet.pk + 999999
        self.assertFalse(Wallet.objects.filter(pk=bogus_id).exists())
        data = _minimal_valid_data(wallet)
        data["wallet"] = bogus_id
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("wallet", form.errors)

    def test_wallet_label_shows_username_and_email(self):
        user = make_user(username="o3a3_lookup_user", email="o3a3@example.com")
        wallet = make_wallet(user=user)
        form = TreasuryOperationRequestForm()
        label = form.fields["wallet"].label_from_instance(wallet)
        self.assertIn("o3a3_lookup_user", label)
        self.assertIn("o3a3@example.com", label)

    def test_wallet_label_falls_back_to_username_without_email(self):
        user = make_user(username="o3a3_no_email_user", email="")
        wallet = make_wallet(user=user)
        form = TreasuryOperationRequestForm()
        label = form.fields["wallet"].label_from_instance(wallet)
        self.assertEqual(label, "o3a3_no_email_user")

    def test_wallet_str_was_not_modified(self):
        # Explicit guard per the O.3a-3 instructions: Wallet.__str__ must
        # remain untouched by this block.
        user = make_user(username="o3a3_str_guard")
        wallet = make_wallet(user=user)
        self.assertEqual(str(wallet), f"Wallet({user.id}) {wallet.currency} avail={wallet.available_balance}")


class GeneralValidationTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def test_operation_type_required(self):
        data = _minimal_valid_data(self.wallet, operation_type="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("operation_type", form.errors)

    def test_operation_type_must_be_one_of_the_six_choices(self):
        data = _minimal_valid_data(self.wallet, operation_type="TREASURY_HOLD")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("operation_type", form.errors)

    def test_amount_required(self):
        data = _minimal_valid_data(self.wallet)
        del data["amount"]
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)

    def test_amount_zero_rejected(self):
        data = _minimal_valid_data(self.wallet, amount="0.00")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)

    def test_amount_negative_rejected(self):
        data = _minimal_valid_data(self.wallet, amount="-10.00")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)

    def test_amount_more_than_two_decimals_rejected(self):
        data = _minimal_valid_data(self.wallet, amount="10.999")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)

    def test_amount_with_exactly_two_decimals_accepted(self):
        data = _minimal_valid_data(self.wallet, amount="10.99")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["amount"], Decimal("10.99"))

    def test_reason_required(self):
        data = _minimal_valid_data(self.wallet, reason="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reason", form.errors)

    def test_reason_whitespace_only_rejected(self):
        data = _minimal_valid_data(self.wallet, reason="     ")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reason", form.errors)

    def test_reason_is_stripped(self):
        data = _minimal_valid_data(self.wallet, reason="  padded reason  ")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["reason"], "padded reason")

    def test_reference_is_stripped_when_provided(self):
        data = _minimal_valid_data(self.wallet, reference="  TCK-42  ")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["reference"], "TCK-42")


class CreditFundsTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_CREDIT_FUNDS,
            "amount": "100.00",
            "reason": "Compensation for system error",
            "category": TreasuryOperationRequest.CAT_OTHER,
        }
        data.update(overrides)
        return data

    def test_valid_credit_funds_minimal(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_category_required(self):
        data = self._data(category="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)

    def test_reference_required_when_category_system_error(self):
        data = self._data(category=TreasuryOperationRequest.CAT_SYSTEM_ERROR, reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_reference_required_when_category_provider_duplicate(self):
        data = self._data(category=TreasuryOperationRequest.CAT_PROVIDER_DUPLICATE, reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_reference_not_required_when_category_other(self):
        data = self._data(category=TreasuryOperationRequest.CAT_OTHER, reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_reference_provided_and_valid_when_category_system_error(self):
        data = self._data(
            category=TreasuryOperationRequest.CAT_SYSTEM_ERROR, reference="TCK-1001",
        )
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)


class DebitFundsTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet(initial_balance=Decimal("10.00"))

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_DEBIT_FUNDS,
            "amount": "100.00",
            "reason": "Reverse erroneous credit",
            "category": TreasuryOperationRequest.CAT_OTHER,
        }
        data.update(overrides)
        return data

    def test_valid_debit_funds_minimal(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_category_required(self):
        data = self._data(category="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)

    def test_reference_required_when_category_provider_duplicate(self):
        data = self._data(category=TreasuryOperationRequest.CAT_PROVIDER_DUPLICATE, reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_amount_exceeding_wallet_balance_is_still_valid(self):
        self.assertEqual(self.wallet.available_balance, Decimal("10.00"))
        data = self._data(amount="999999.00")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(
            form.is_valid(), f"Balance sufficiency must not be enforced here: {form.errors}",
        )
        self.assertEqual(form.cleaned_data["amount"], Decimal("999999.00"))


class RefundTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_REFUND,
            "amount": "75.00",
            "reason": "Refund duplicate deposit",
            "reference": "DEP-555",
        }
        data.update(overrides)
        return data

    def test_valid_refund_minimal(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_reference_required(self):
        data = self._data(reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_category_comment_evidence_optional(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data.get("category"), "")
        self.assertEqual(form.cleaned_data.get("comment"), "")
        self.assertFalse(form.cleaned_data.get("evidence"))


class BonusCreditTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_BONUS_CREDIT,
            "amount": "20.00",
            "reason": "Welcome bonus campaign",
        }
        data.update(overrides)
        return data

    def test_valid_bonus_credit_minimal(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_category_reference_comment_evidence_all_optional(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_no_maximum_amount_limit_enforced_yet(self):
        # O.3a-3 explicitly does not implement configurable maximums.
        data = self._data(amount="1000000.00")
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)


class IBCommissionTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_IB_COMMISSION,
            "amount": "15.00",
            "reason": "Monthly IB commission payout",
            "reference": "IB-PERIOD-2026-07",
        }
        data.update(overrides)
        return data

    def test_valid_ib_commission_minimal(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_reference_required(self):
        data = self._data(reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_wallet_user_without_any_referral_is_still_valid(self):
        # No Referral-active check exists yet, by design — the wallet's
        # user here has zero Referral rows and the form must not care.
        from simulator.models import Referral
        self.assertFalse(Referral.objects.filter(user=self.wallet.user).exists())
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)


class ManualAdjustmentTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        data = {
            "wallet": self.wallet.pk,
            "operation_type": TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
            "amount": "33.00",
            "reason": "One-off adjustment per support ticket",
            "category": TreasuryOperationRequest.CAT_OTHER,
            "reference": "SUP-9001",
            "comment": "Approved verbally by ops lead, documented here.",
        }
        data.update(overrides)
        return data

    def test_valid_manual_adjustment_full(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_category_required(self):
        data = self._data(category="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)

    def test_reference_required(self):
        data = self._data(reference="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_comment_required(self):
        data = self._data(comment="")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("comment", form.errors)

    def test_comment_whitespace_only_rejected(self):
        data = self._data(comment="     ")
        form = TreasuryOperationRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("comment", form.errors)

    def test_evidence_not_required_yet(self):
        data = self._data()
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data.get("evidence"))


class EvidenceValidationTests(TestCase):

    def setUp(self):
        self.wallet = make_wallet()

    def _data(self, **overrides):
        return _minimal_valid_data(self.wallet, **overrides)

    def test_whitelist_constants_match_spec(self):
        self.assertEqual(
            TREASURY_EVIDENCE_ALLOWED_EXTENSIONS, {"pdf", "jpg", "jpeg", "png"},
        )
        self.assertEqual(
            TREASURY_EVIDENCE_ALLOWED_CONTENT_TYPES,
            {"application/pdf", "image/jpeg", "image/png"},
        )
        self.assertEqual(TREASURY_EVIDENCE_MAX_SIZE_BYTES, 5 * 1024 * 1024)

    def test_valid_pdf_accepted(self):
        f = SimpleUploadedFile("proof.pdf", b"%PDF-1.4 fake content", content_type="application/pdf")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_jpg_accepted(self):
        f = SimpleUploadedFile("proof.jpg", b"\xff\xd8\xff fake jpg", content_type="image/jpeg")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_jpeg_accepted(self):
        f = SimpleUploadedFile("proof.jpeg", b"\xff\xd8\xff fake jpeg", content_type="image/jpeg")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_png_accepted(self):
        f = SimpleUploadedFile("proof.png", b"\x89PNG fake png", content_type="image/png")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_disallowed_extension_rejected(self):
        f = SimpleUploadedFile("malware.exe", b"MZ fake exe", content_type="application/octet-stream")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertFalse(form.is_valid())
        self.assertIn("evidence", form.errors)

    def test_disallowed_extension_zip_rejected(self):
        f = SimpleUploadedFile("archive.zip", b"PK fake zip", content_type="application/zip")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertFalse(form.is_valid())
        self.assertIn("evidence", form.errors)

    def test_mismatched_content_type_rejected(self):
        # .pdf extension but a disallowed content_type — second layer catches it.
        f = SimpleUploadedFile("proof.pdf", b"fake", content_type="application/x-msdownload")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertFalse(form.is_valid())
        self.assertIn("evidence", form.errors)

    def test_file_too_large_rejected(self):
        too_big = b"0" * (TREASURY_EVIDENCE_MAX_SIZE_BYTES + 1)
        f = SimpleUploadedFile("proof.pdf", too_big, content_type="application/pdf")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertFalse(form.is_valid())
        self.assertIn("evidence", form.errors)

    def test_file_at_exact_limit_accepted(self):
        exactly_max = b"0" * TREASURY_EVIDENCE_MAX_SIZE_BYTES
        f = SimpleUploadedFile("proof.pdf", exactly_max, content_type="application/pdf")
        form = TreasuryOperationRequestForm(data=self._data(), files={"evidence": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_evidence_optional_when_absent(self):
        form = TreasuryOperationRequestForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)


class SaveCommitFalseTests(TestCase):
    """
    Único test de save() permitido en este bloque: comprueba que
    save(commit=False) no llena campos protegidos automáticamente.
    Nunca se llama con commit=True — no se persiste nada.
    """

    def test_save_commit_false_does_not_populate_protected_fields(self):
        wallet = make_wallet()
        data = _minimal_valid_data(wallet)
        form = TreasuryOperationRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

        instance = form.save(commit=False)

        self.assertIsNone(instance.pk)
        self.assertEqual(instance.status, TreasuryOperationRequest.ST_PENDING)
        self.assertIsNone(instance.requested_by)
        self.assertIsNone(instance.requested_at)
        self.assertIsNone(instance.approved_by)
        self.assertIsNone(instance.rejected_by)
        self.assertIsNone(instance.executed_by)
        self.assertIsNone(instance.wallet_transaction)
        self.assertEqual(instance.currency, "")
        self.assertEqual(instance.metadata, {})
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)


class NoProductiveSideEffectsTests(TestCase):
    """
    Confirma que, tras ejercitar el formulario extensivamente en este
    archivo, nada quedó persistido — ni siquiera por accidente.

    O.3a-3 en sí no creó ninguna URL/vista/template — esas llegaron en
    O.3a-5 (admin:treasury_request_new), que es donde ahora se prueban.
    """

    def test_no_treasury_operation_request_rows_exist(self):
        self.assertEqual(TreasuryOperationRequest.objects.count(), 0)

    def test_treasuryoperationrequestadmin_still_blocks_add(self):
        from django.contrib import admin as django_admin
        from simulator.models import TreasuryOperationRequest as TOR
        model_admin = django_admin.site._registry[TOR]
        self.assertFalse(model_admin.has_add_permission(request=None))
