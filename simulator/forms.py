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
    """Crypto withdrawal request — amount in USD + destination address."""

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
            "id": "id_wd_amount",
        }),
    )
    crypto_currency = forms.ChoiceField(
        label="Criptomoneda",
        widget=forms.Select(attrs={"class": "deposit-input", "id": "id_wd_crypto"}),
    )
    wallet_address = forms.CharField(
        label="Dirección destino",
        max_length=200,
        widget=forms.TextInput(attrs={
            "class": "deposit-input",
            "placeholder": "Dirección de tu wallet personal",
            "id": "id_wd_address",
            "autocomplete": "off",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .currencies import WITHDRAWAL_CHOICES
        self.fields["crypto_currency"].choices = WITHDRAWAL_CHOICES

    def clean_wallet_address(self):
        addr = self.cleaned_data.get("wallet_address", "").strip()
        if len(addr) < 20:
            raise forms.ValidationError("Dirección inválida (mínimo 20 caracteres).")
        return addr


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
    _CATEGORY_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_CREDIT_FUNDS,
        TreasuryOperationRequest.OP_DEBIT_FUNDS,
        TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
    }
    _REFERENCE_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_REFUND,
        TreasuryOperationRequest.OP_IB_COMMISSION,
        TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
    }
    _COMMENT_REQUIRED_TYPES = {
        TreasuryOperationRequest.OP_MANUAL_ADJUSTMENT,
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
        # reason is required for all six operation_type. The model field
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
            self.add_error("comment", "Comment es obligatorio para Manual Adjustment.")

        return cleaned_data