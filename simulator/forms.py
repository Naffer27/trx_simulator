# simulator/forms.py
from django import forms
from django.contrib.auth.models import User
from django.conf import settings
from .models import TradingAccount


class LoginForm(forms.Form):
    username = forms.CharField(label="Usuario", max_length=150)
    password = forms.CharField(label="Contraseña", widget=forms.PasswordInput)
    # por defecto opcional; se vuelve requerido si hay BROKER_ACCESS_CODE y no estás en DEBUG
    access_code = forms.CharField(label="Código de acceso", max_length=50, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        require_code = bool(getattr(settings, "BROKER_ACCESS_CODE", "").strip()) and not settings.DEBUG
        self.fields["access_code"].required = require_code
        self.fields["access_code"].help_text = (
            "Requerido por el administrador." if require_code else "Opcional (modo desarrollo)."
        )


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
    tier = forms.ChoiceField(
        choices=[("10K", "10K"), ("50K", "50K"), ("100K", "100K")],
        label="Nivel de fondeo"
    )
    phase = forms.ChoiceField(
        choices=[("Fase 1", "Fase 1"), ("Fase 2", "Fase 2")],
        label="Fase inicial"
    )

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2", "tier", "phase")

    # Validaciones útiles
    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")
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