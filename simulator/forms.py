# simulator/forms.py
from decimal import Decimal
from django import forms
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from .models import (
    TradingAccount, Deposit, MARGIN_ENGINE_TYPES, KYCProfile,
    TreasuryOperationRequest, Wallet,
)


class LoginForm(forms.Form):
    username = forms.CharField(label="Usuario", max_length=150)
    password = forms.CharField(label="Contraseña", widget=forms.PasswordInput)


class TradingAccountForm(forms.ModelForm):
    class Meta:
        model = TradingAccount
        fields = [
            'tier',
            'phase',
            'balance',
            'profit_target',
            'max_drawdown',
        ]
        widgets = {
            'tier': forms.Select(attrs={'class': 'form-control'}),
            'phase': forms.Select(attrs={'class': 'form-control'}),
            'balance': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'profit_target': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'max_drawdown': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }


# ➕ Formulario de registro de usuarios (modo simple)
class RegisterForm(forms.ModelForm):
    password1 = forms.CharField(label="Contraseña", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirmar contraseña", widget=forms.PasswordInput)
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if getattr(settings, "BROKER_ACCESS_CODE", "").strip():
            self.fields["access_code"] = forms.CharField(
                label="Access Code",
                max_length=128,
                widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                required=True,
            )

    def clean_access_code(self):
        import secrets as _secrets
        expected = getattr(settings, "BROKER_ACCESS_CODE", "").strip()
        submitted = self.cleaned_data.get("access_code", "")
        if not _secrets.compare_digest(submitted.encode(), expected.encode()):
            raise forms.ValidationError("Invalid access code.")
        return ""  # never propagate the raw code into cleaned_data

    # Validaciones útiles
    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")
        validate_password(p2)
        return p2

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe un usuario con este email.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class DepositForm(forms.Form):
    # $20 floor covers NowPayments minimum for BTC (~$19.19) and all other currencies.
    amount_usd = forms.DecimalField(
        label="Monto (USD)",
        min_value=Decimal("20"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "deposit-input",
            "min": "20",
            "step": "1",
            "placeholder": "Mínimo $20",
            "id": "id_amount_usd",
        }),
    )
    crypto_currency = forms.ChoiceField(
        label="Criptomoneda",
        choices=Deposit.CRYPTO_CHOICES,
        widget=forms.Select(attrs={"class": "deposit-input", "id": "id_crypto_currency"}),
    )


class WithdrawForm(forms.Form):
    """
    Crypto withdrawal request — first step of the WITHDRAWAL-SECURITY-EXTENSION-01
    two-step flow (this form only creates a WithdrawalEmailOTPChallenge; the
    WithdrawalRequest itself is created later, from the challenge's frozen
    payload, by WithdrawOTPVerifyForm — see views.withdraw_otp_verify_view).

    amount_usd is required unless withdraw_all is checked — Wallet.available_balance
    is what actually gets debited for withdraw_all (see wallet_ledger.debit_wallet
    call site), never a client-supplied amount.

    wallet_address is a choice of the user's own ACTIVE VerifiedWithdrawalWallet
    rows — Design Lock rule 4 ("no se ofrece texto libre"): the destination
    must already be a verified/authorized wallet, never typed at withdrawal time.
    """

    amount_usd = forms.DecimalField(
        label="Monto (USD)",
        required=False,
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "deposit-input",
            "step": "1",
            "id": "id_wd_amount",
        }),
    )
    withdraw_all = forms.BooleanField(
        label="Retirar todo el balance disponible",
        required=False,
        widget=forms.CheckboxInput(attrs={"id": "id_wd_all"}),
    )
    crypto_currency = forms.ChoiceField(
        label="Criptomoneda",
        widget=forms.Select(attrs={"class": "deposit-input", "id": "id_wd_crypto"}),
    )
    wallet_address = forms.ChoiceField(
        label="Wallet destino verificada",
        widget=forms.Select(attrs={"class": "deposit-input", "id": "id_wd_address"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        from .currencies import WITHDRAWAL_CURRENCY_MAP
        enabled = [
            k for k in settings.WALLET_WITHDRAWAL_ENABLED_ASSETS
            if k in WITHDRAWAL_CURRENCY_MAP
        ]
        self.fields["crypto_currency"].choices = [
            (k, WITHDRAWAL_CURRENCY_MAP[k][1]) for k in enabled
        ]

        wallet_choices = []
        if user is not None:
            from .models import VerifiedWithdrawalWallet
            from .verified_wallets import get_active_wallet
            from .address_validators import ASSET_NETWORK_BY_CURRENCY
            # Lazily activate any due PENDING_COOLDOWN row before listing —
            # same lookup withdraw_otp_verify_view uses at submission time.
            seen = set()
            for asset, network in ASSET_NETWORK_BY_CURRENCY.values():
                w = get_active_wallet(user, asset=asset, network=network)
                if w is not None and w.pk not in seen:
                    seen.add(w.pk)
                    label = f"{w.asset}/{w.network} — {w.address[:6]}…{w.address[-4:]}"
                    wallet_choices.append((str(w.pk), label))
        self.fields["wallet_address"].choices = wallet_choices

    def clean(self):
        cleaned = super().clean()
        withdraw_all = cleaned.get("withdraw_all")
        amount_usd = cleaned.get("amount_usd")

        if not withdraw_all and amount_usd is None:
            self.add_error("amount_usd", "Monto requerido (o marca 'Retirar todo').")

        crypto_currency = cleaned.get("crypto_currency")
        wallet_pk = cleaned.get("wallet_address")
        if crypto_currency and wallet_pk and self.user is not None:
            from .models import VerifiedWithdrawalWallet
            from .address_validators import ASSET_NETWORK_BY_CURRENCY
            try:
                vw = VerifiedWithdrawalWallet.objects.get(
                    pk=wallet_pk, user=self.user, status=VerifiedWithdrawalWallet.STATUS_ACTIVE,
                )
            except (VerifiedWithdrawalWallet.DoesNotExist, ValueError, TypeError):
                self.add_error("wallet_address", "Wallet inválida o no verificada.")
            else:
                expected = ASSET_NETWORK_BY_CURRENCY.get(crypto_currency)
                if expected != (vw.asset, vw.network):
                    self.add_error(
                        "wallet_address",
                        "La wallet seleccionada no corresponde a la moneda elegida.",
                    )
                else:
                    cleaned["verified_wallet"] = vw
        return cleaned


class WithdrawOTPVerifyForm(forms.Form):
    """
    Second step of the withdrawal flow — ONLY a challenge id + the 6-digit
    code. Deliberately carries no amount/asset/network/address fields: the
    WithdrawalRequest is built exclusively from the challenge's frozen
    payload (Design Lock rule 3 — "no permitir cambiar amount/wallet/network
    después del OTP sin crear un challenge nuevo").
    """
    challenge_id = forms.IntegerField(widget=forms.HiddenInput())
    code = forms.CharField(
        label="Código de verificación",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            "class": "deposit-input", "id": "id_wd_otp_code",
            "autocomplete": "one-time-code", "inputmode": "numeric",
        }),
    )

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code.isdigit():
            raise forms.ValidationError("El código debe ser numérico.")
        return code


class RegisterWithdrawalWalletForm(forms.Form):
    """
    First step of the wallet change/registration flow (Design Lock rule 6):
    picks the asset + types the new address. TOTP is required at this step
    (checked in the view, same gate style as WithdrawForm); a
    WithdrawalEmailOTPChallenge(purpose=ADDRESS_CHANGE) is created next.
    """
    asset = forms.ChoiceField(label="Criptomoneda")
    address = forms.CharField(
        label="Nueva dirección de retiro",
        max_length=200,
        widget=forms.TextInput(attrs={"class": "deposit-input", "autocomplete": "off"}),
    )
    otp_code = forms.CharField(label="Código TOTP", max_length=10)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .currencies import WITHDRAWAL_CURRENCY_MAP
        enabled = [
            k for k in settings.WALLET_WITHDRAWAL_ENABLED_ASSETS
            if k in WITHDRAWAL_CURRENCY_MAP
        ]
        self.fields["asset"].choices = [(k, WITHDRAWAL_CURRENCY_MAP[k][1]) for k in enabled]

    def clean(self):
        cleaned = super().clean()
        currency_key = cleaned.get("asset")
        address = (cleaned.get("address") or "").strip()
        if currency_key and address:
            from .address_validators import validate_address_for_currency
            if not validate_address_for_currency(currency_key, address):
                self.add_error("address", "Dirección inválida para la red seleccionada.")
        return cleaned


# ──────────────────────────────────────────────────────────────
# Wallet / Account management forms
# ──────────────────────────────────────────────────────────────

class CreateAccountForm(forms.Form):
    """Create a new trading account funded from the user's wallet."""

    # Only margin-engine (real broker) types are user-selectable.
    # CHALLENGE/FUNDED accounts are created through the purchase flow.
    ACCOUNT_TYPE_CHOICES = [
        ("RETAIL",   "Retail — margin engine, leverage, liquidation"),
        ("ECN",      "ECN — tighter spreads, commission-based"),
        ("STANDARD", "Standard — normal spreads, no commission"),
        ("DEMO",     "Demo — practice account with virtual $10,000"),
        ("CRYPTO",   "Crypto — crypto-focused, higher leverage"),
    ]

    LEVERAGE_CHOICES = [
        (50,   "1:50"),
        (100,  "1:100"),
        (200,  "1:200"),
        (500,  "1:500"),
    ]

    account_type = forms.ChoiceField(
        choices=ACCOUNT_TYPE_CHOICES,
        widget=forms.Select(attrs={"class": "form-input"}),
    )
    initial_deposit = forms.DecimalField(
        label="Initial deposit (USD)",
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=2,
        required=False,
        initial=Decimal("0"),
        widget=forms.NumberInput(attrs={
            "class": "form-input", "step": "1", "min": "0", "placeholder": "0.00",
        }),
    )
    leverage = forms.ChoiceField(
        choices=LEVERAGE_CHOICES,
        initial=100,
        widget=forms.Select(attrs={"class": "form-input"}),
    )

    def clean(self):
        cleaned = super().clean()
        acct = cleaned.get("account_type")
        deposit = cleaned.get("initial_deposit") or Decimal("0")
        if acct != "DEMO" and deposit <= 0:
            raise forms.ValidationError(
                "Initial deposit is required for non-Demo accounts."
            )
        return cleaned


class FundAccountForm(forms.Form):
    """Transfer funds from wallet into an existing trading account."""
    amount = forms.DecimalField(
        label="Amount (USD)",
        min_value=Decimal("1"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-input", "step": "1", "min": "1", "placeholder": "100.00",
        }),
    )


class WithdrawAccountForm(forms.Form):
    """Transfer funds from a trading account back to the wallet."""
    amount = forms.DecimalField(
        label="Amount (USD)",
        min_value=Decimal("1"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            "class": "form-input", "step": "1", "min": "1", "placeholder": "100.00",
        }),
    )


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(
        label="Nombre", max_length=150, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Nombre"}),
    )
    last_name = forms.CharField(
        label="Apellido", max_length=150, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Apellido"}),
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name"]


class KYCProfileForm(forms.ModelForm):
    class Meta:
        model  = KYCProfile
        fields = [
            "legal_name",
            "country",
            "document_type",
            "document_number",
            "document_front",
            "document_back",
            "selfie",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["legal_name"].required     = True
        self.fields["country"].required        = True
        self.fields["document_type"].required  = True
        self.fields["document_front"].required = True
        self.fields["document_number"].required = False
        self.fields["document_back"].required   = False
        self.fields["selfie"].required          = False


# ─────────────────────────────────────────────
# Treasury Private Operations — O.3a-3
#
# Isolated form only. No view, URL, template or productive save() exists
# yet — this class is validated directly via is_valid()/cleaned_data in
# tests. Nothing here creates an AuditLog or BrokerAuditEvent row, and
# nothing here is wired into admin.py.
# ─────────────────────────────────────────────

class WalletChoiceField(forms.ModelChoiceField):
    """
    Human-readable wallet lookup for the operator (username/email),
    instead of a raw wallet_id as the primary UX — without touching
    Wallet.__str__ (used elsewhere, not authorized to change in this
    block). Standard ModelChoiceField already gives "wallet obligatorio
    y existente" for free: required=True is inferred from the model's
    non-nullable FK, and its queryset makes any non-existent/deleted
    wallet id fail validation automatically. This subclass only
    overrides the display label.
    """
    def label_from_instance(self, obj):
        email = (obj.user.email or "").strip()
        if email:
            return f"{obj.user.username} ({email})"
        return obj.user.username


# Evidence whitelist — conservative first pass. Extension is the primary
# gate (client-supplied content_type is never trusted alone); content_type
# is checked as a second layer only when the uploaded file actually
# provides one.
TREASURY_EVIDENCE_ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
TREASURY_EVIDENCE_ALLOWED_CONTENT_TYPES = {
    "application/pdf", "image/jpeg", "image/png",
}
TREASURY_EVIDENCE_MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


class TreasuryOperationRequestForm(forms.ModelForm):
    """
    O.3a-3 — isolated TreasuryOperationRequest submission form.

    Exposed fields (Meta.fields, operator input): wallet, operation_type,
    amount, reason, reference, category, comment, evidence.

    Deliberately NOT exposed (never settable by the operator through this
    form): currency (derived from wallet.currency by a later block, never
    operator input), status, metadata, wallet_transaction, requested_by,
    requested_at, approved_by, approved_at, rejected_by, rejected_at,
    rejection_reason, executed_by, executed_at, failure_reason,
    cancelled_at, updated_at.
    """
    wallet = WalletChoiceField(
        queryset=Wallet.objects.select_related("user"),
        label="Wallet",
    )

    class Meta:
        model = TreasuryOperationRequest
        fields = [
            "wallet", "operation_type", "amount", "reason",
            "reference", "category", "comment", "evidence",
        ]

    # Per-operation_type requirement tables (frozen O.3a architecture,
    # Fase 0 §2/§4) — enforced here, in the service layer, never in the
    # schema, same discipline TreasuryOperationRequest's own docstring
    # already documents for reference/category.
    #
    # O.3c-0a — OP_MANUAL_ADJUSTMENT was replaced by OP_MANUAL_CREDIT /
    # OP_MANUAL_DEBIT (frozen O.3c-0 architecture decision: the type
    # itself carries direction, no `direction` field). Both new types
    # inherit MANUAL_ADJUSTMENT's exact requirement rules unchanged —
    # they differ only in which wallet_ledger.py primitive the future
    # execution engine calls, never in what this form requires.
    _CATEGORY_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_CREDIT_FUNDS,
        TreasuryOperationRequest.OP_DEBIT_FUNDS,
        TreasuryOperationRequest.OP_MANUAL_CREDIT,
        TreasuryOperationRequest.OP_MANUAL_DEBIT,
    }
    _REFERENCE_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_REFUND,
        TreasuryOperationRequest.OP_IB_COMMISSION,
        TreasuryOperationRequest.OP_MANUAL_CREDIT,
        TreasuryOperationRequest.OP_MANUAL_DEBIT,
    }
    _COMMENT_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_MANUAL_CREDIT,
        TreasuryOperationRequest.OP_MANUAL_DEBIT,
    }
    # CREDIT_FUNDS / DEBIT_FUNDS only: reference additionally becomes
    # required when category is one of these two.
    _REFERENCE_REQUIRED_CATEGORIES = {
        TreasuryOperationRequest.CAT_SYSTEM_ERROR,
        TreasuryOperationRequest.CAT_PROVIDER_DUPLICATE,
    }
    _REFERENCE_CATEGORY_GATED_TYPES = {
        TreasuryOperationRequest.OP_CREDIT_FUNDS,
        TreasuryOperationRequest.OP_DEBIT_FUNDS,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # reason is required for all operation_type values. The model field
        # is blank=True (per-type enforcement lives in the form, never in
        # the schema), so ModelForm would otherwise default it to
        # required=False.
        self.fields["reason"].required = True

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("El monto debe ser mayor a cero.")
        return amount

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Reason es obligatorio.")
        return reason

    def clean_reference(self):
        return (self.cleaned_data.get("reference") or "").strip()

    def clean_comment(self):
        return (self.cleaned_data.get("comment") or "").strip()

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if not evidence:
            return evidence

        name = getattr(evidence, "name", "") or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TREASURY_EVIDENCE_ALLOWED_EXTENSIONS:
            raise forms.ValidationError(
                f"Tipo de archivo no permitido (.{ext or '?'}). "
                "Solo se aceptan PDF, JPG, JPEG o PNG."
            )

        content_type = getattr(evidence, "content_type", None)
        if content_type and content_type not in TREASURY_EVIDENCE_ALLOWED_CONTENT_TYPES:
            raise forms.ValidationError(
                f"Tipo de contenido no permitido ({content_type})."
            )

        size = getattr(evidence, "size", None)
        if size is not None and size > TREASURY_EVIDENCE_MAX_SIZE_BYTES:
            raise forms.ValidationError(
                "El archivo excede el tamaño máximo permitido (5 MB)."
            )

        return evidence

    def clean(self):
        cleaned_data = super().clean()
        operation_type = cleaned_data.get("operation_type")
        category = cleaned_data.get("category")
        reference = cleaned_data.get("reference")
        comment = cleaned_data.get("comment")

        if operation_type in self._CATEGORY_REQUIRED_TYPES and not category:
            self.add_error(
                "category", "Category es obligatoria para este tipo de operación.",
            )

        reference_required = operation_type in self._REFERENCE_REQUIRED_TYPES or (
            operation_type in self._REFERENCE_CATEGORY_GATED_TYPES
            and category in self._REFERENCE_REQUIRED_CATEGORIES
        )
        if reference_required and not reference:
            self.add_error(
                "reference",
                "Reference es obligatoria para este tipo de operación/categoría.",
            )

        if operation_type in self._COMMENT_REQUIRED_TYPES and not comment:
            self.add_error("comment", "Comment es obligatorio para Manual Credit/Debit Adjustment.")

        return cleaned_data