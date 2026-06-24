# simulator/forms.py
from decimal import Decimal
from django import forms
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from .models import TradingAccount, Deposit, MARGIN_ENGINE_TYPES, KYCProfile


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