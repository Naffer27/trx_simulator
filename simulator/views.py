# simulator/views.py
from decimal import Decimal
from django.utils import timezone
from django.shortcuts import render, redirect
from django.core.serializers.json import DjangoJSONEncoder
from django.contrib.auth import authenticate, login as auth_login, logout
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.conf import settings
from django.db.models import Sum, Count, Max, Min, Q
from django.db import transaction
from django.urls import reverse
import json, random, logging, time

from .models import (
    Purchase, TradingAccount, Trade, Position, LedgerEntry, Deposit,
    WalletTransaction, WithdrawalRequest, MARGIN_ENGINE_TYPES,
)
from .forms import LoginForm, RegisterForm, DepositForm, WithdrawForm, CreateAccountForm, FundAccountForm, WithdrawAccountForm
from .wallet_ledger import credit_wallet, debit_wallet, transfer_to_account, transfer_to_wallet, get_or_create_wallet, InsufficientFunds
from .currencies import to_np_code, CURRENCY_MAP

logger = logging.getLogger(__name__)


def landing_view(request):
    return render(request, 'simulator/landing.html')


# ===== Configuración de mercado (simulada) =====
# Prices match base_price_for() in consumers.py so HTTP-created positions
# don't trigger SL/TP immediately when the WS loads them at live price.
SYMBOL_BASE_PRICES = {
    "EUR/USD": Decimal("1.17000"),
    "GBP/USD": Decimal("1.30000"),
    "USD/JPY": Decimal("155.000"),
    "AUD/USD": Decimal("0.68000"),
    "BTCUSD":  Decimal("68000.0"),
    "ETHUSD":  Decimal("3400.0"),
}
_DEFAULT_BASE = Decimal("1.17000")

SPREAD = Decimal("0.00020")
SLIPPAGE = Decimal("0.00010")


def get_base_price(symbol: str) -> Decimal:
    return SYMBOL_BASE_PRICES.get(symbol, _DEFAULT_BASE)


def apply_spread_and_slippage(base_price, side):
    if side.upper() == "BUY":
        px = base_price + (SPREAD / 2)
    else:
        px = base_price - (SPREAD / 2)

    slip = Decimal(str(random.uniform(float(-SLIPPAGE), float(SLIPPAGE))))
    px = px + slip
    return px.quantize(Decimal('0.00001'))


# -----------------------
# REGISTRO DE USUARIO
# -----------------------
@csrf_exempt
def register_view(request):
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            tier = form.cleaned_data["tier"]
            phase = form.cleaned_data["phase"]

            # Crear Purchase asociado al usuario con código disponible
            purchase = Purchase.objects.create(
                user=user,
                tier=tier,
                code=f"CODE-{user.id}-{random.randint(1000,9999)}",
                used=False
            )

            # Crear TradingAccount asociada al usuario
            base_balance = 10000 if tier == "10K" else (50000 if tier == "50K" else 100000)
            account = TradingAccount.objects.create(
                user=user,
                tier=tier,
                phase=phase,
                balance=base_balance,
                equity=base_balance,
                status="Activo"
            )

            # Guardar en sesión y login
            request.session['account_id'] = account.id
            auth_login(request, user)

            # =====================
            # 📧 Correo HTML al usuario con código
            # =====================
            subject_user = "🎉 Bienvenido a TRX Found — Tu Cuenta de Fondeo"
            message_user = f"""
Hola {user.username},

Tu cuenta de fondeo ha sido creada exitosamente 🚀

Detalles de tu cuenta:
- Usuario: {user.username}
- Balance inicial: {base_balance}
- Nivel: {tier}
- Fase: {phase}
- Código de acceso: {purchase.code}

Ya puedes iniciar sesión y acceder al simulador.
"""

            html_message_user = f"""
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: Arial, sans-serif;
      background: #f9f9f9;
      color: #333;
      margin: 0; padding: 0;
    }}
    .container {{
      max-width: 600px; margin: 40px auto; background: #fff;
      border-radius: 12px; overflow: hidden;
      box-shadow: 0 4px 10px rgba(0,0,0,0.1);
    }}
    .header {{
      background: linear-gradient(135deg, #ffd700, #ffb300);
      color: #000; text-align: center; padding: 20px;
    }}
    .header h1 {{ margin: 0; font-size: 24px; font-weight: bold; }}
    .content {{ padding: 20px; line-height: 1.6; }}
    .highlight {{
      background: #fffae6;
      border-left: 4px solid #ffb300;
      padding: 10px; margin: 20px 0;
      border-radius: 6px;
    }}
    .footer {{
      text-align: center; padding: 15px; font-size: 12px; color: #666;
      border-top: 1px solid #eee;
    }}
    .btn {{
      display: inline-block; padding: 12px 20px;
      background: #ffb300; color: #000; font-weight: bold;
      text-decoration: none; border-radius: 6px;
      margin-top: 20px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🚀 Bienvenido a TRX Found</h1>
    </div>
    <div class="content">
      <p>Hola <strong>{user.username}</strong>,</p>
      <p>Tu cuenta de fondeo ha sido creada exitosamente. Aquí tienes los detalles:</p>
      
      <div class="highlight">
        <p><strong>Balance inicial:</strong> {base_balance}</p>
        <p><strong>Nivel:</strong> {tier}</p>
        <p><strong>Fase:</strong> {phase}</p>
        <p><strong>Código de acceso:</strong> {purchase.code}</p>
      </div>

      <p>Ya puedes iniciar sesión en el simulador y comenzar tu challenge.</p>

      <p style="text-align:center;">
        <a href="http://127.0.0.1:8000/login/" class="btn">Entrar al Simulador</a>
      </p>
    </div>
    <div class="footer">
      © 2025 TRX Found · Este es un correo automático, por favor no responder.
    </div>
  </div>
</body>
</html>
"""

            send_mail(
                subject_user,
                message_user,  # fallback texto plano
                "nafferphotographer@gmail.com",
                [user.email],
                fail_silently=True,
                html_message=html_message_user
            )

            # 📧 Correo al admin
            subject_admin = "📢 Nuevo usuario registrado"
            message_admin = f"""
Un nuevo usuario ha creado cuenta:

- Usuario: {user.username}
- Email: {user.email}
- Balance inicial: {base_balance}
- Nivel: {tier}
- Fase: {phase}
- Código: {purchase.code}
"""
            send_mail(
                subject_admin,
                message_admin,
                "nafferphotographer@gmail.com",
                ["nafferphotographer@gmail.com"],
                fail_silently=True,
            )

            return redirect("simulator:login")
    else:
        form = RegisterForm()
    return render(request, "simulator/register.html", {"form": form})


# -----------------------
# LOGIN CON CÓDIGO DE ACCESO (flexible)
# -----------------------
def login_view(request):
    """
    En DEV (DEBUG=True) o si BROKER_ACCESS_CODE está vacío → el código es opcional.
    En PROD, si BROKER_ACCESS_CODE tiene valor → se acepta ese código global
    o el código de Purchase del usuario.
    """
    error = None
    code_prefill = request.GET.get('code', None)

    if request.method == 'POST':
        username    = request.POST.get('username', '')
        password    = request.POST.get('password', '')
        access_code = (request.POST.get('access_code') or '').strip()

        user = authenticate(request, username=username, password=password)
        if user is None:
            error = "Usuario o contraseña inválidos"
        else:
            expected_global = (getattr(settings, "BROKER_ACCESS_CODE", "") or "").strip()

            ok_code = False
            purchase = None

            # 1) Si hay código global configurado, lo aceptamos
            if expected_global:
                ok_code = (access_code == expected_global)

            # 2) Si no pasó por el global, probamos contra Purchase del usuario
            if not ok_code and access_code:
                purchase = Purchase.objects.filter(user=user, code=access_code).first()
                ok_code = purchase is not None

            # 3) En DEV o si no hay BROKER_ACCESS_CODE definido y no envían código → permitir
            if not ok_code and (settings.DEBUG or not expected_global) and not access_code:
                ok_code = True  # acceso libre en desarrollo

            if not ok_code:
                error = "Código inválido"
            else:
                auth_login(request, user)
                # Set session to the user's most recent active account if one exists,
                # but never create one here — account management lives in /accounts/.
                active = (
                    TradingAccount.objects
                    .filter(user=user, status="Activo")
                    .order_by("-id")
                    .first()
                )
                if active:
                    request.session["account_id"] = active.id
                return redirect("simulator:home")

    return render(request, 'simulator/login.html', {
        'error': error,
        'code': code_prefill,
        'form': LoginForm(),
    })


# -----------------------
# LOGOUT
# -----------------------
def logout_view(request):
    logout(request)
    return redirect('simulator:login')


# -----------------------
# DASHBOARD
# -----------------------
@login_required
def trading_dashboard(request, account_id=None):
    if account_id:
        account = TradingAccount.objects.filter(pk=account_id, user=request.user).first()
    else:
        account = _resolve_account(request)

    if not account:
        return redirect("simulator:accounts")

    if request.method == 'POST':
        trade_type  = request.POST.get('trade_type', 'BUY').upper()
        symbol      = request.POST.get('symbol', 'EUR/USD')
        lot_size    = Decimal(request.POST.get('volume', '0.01'))

        stop_loss   = request.POST.get('stop_loss')
        take_profit = request.POST.get('take_profit')
        stop_loss   = Decimal(stop_loss) if stop_loss else None
        take_profit = Decimal(take_profit) if take_profit else None

        entry_price = apply_spread_and_slippage(get_base_price(symbol), trade_type)

        pos = Position.objects.create(
            account=account,
            symbol=symbol,
            side=trade_type,
            qty=lot_size,
            avg_price=entry_price,
            sl=stop_loss,
            tp=take_profit,
        )

        closed = False
        pnl = Decimal('0.00')
        exit_price = None

        if stop_loss and ((trade_type == "BUY" and entry_price <= stop_loss) or (trade_type == "SELL" and entry_price >= stop_loss)):
            exit_price = stop_loss
            closed = True
        elif take_profit and ((trade_type == "BUY" and entry_price >= take_profit) or (trade_type == "SELL" and entry_price <= take_profit)):
            exit_price = take_profit
            closed = True

        if closed:
            direction = Decimal('1') if trade_type == "BUY" else Decimal('-1')
            pnl = (exit_price - entry_price) * lot_size * direction
            pnl = pnl.quantize(Decimal('0.01'))

            Trade.objects.create(
                account=account,
                symbol=symbol,
                trade_type=trade_type,
                lot_size=lot_size,
                entry_price=entry_price,
                exit_price=exit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                profit_loss=pnl,
                opened_at=pos.opened_at,
                closed_at=timezone.now(),
            )

            new_balance = account.balance + pnl
            LedgerEntry.objects.create(
                account=account,
                event_type=LedgerEntry.EV_REALIZED,
                amount=pnl,
                balance_after=new_balance,
                meta={"symbol": symbol, "side": trade_type, "lots": str(lot_size)}
            )
            account.balance = new_balance
            account.equity = new_balance
            account.save()
            pos.delete()

        return redirect('simulator:dashboard')

    trades = Trade.objects.filter(account=account).order_by('-opened_at')[:10]
    open_trades = Trade.objects.filter(account=account).values(
        'entry_price', 'stop_loss', 'take_profit',
        'trade_type', 'symbol', 'lot_size',
        'opened_at', 'exit_price',
    )
    formatted_trades = []
    for t in open_trades:
        formatted_trades.append({
            'entry_price': float(t['entry_price']),
            'stop_loss'  : float(t['stop_loss']) if t['stop_loss'] else None,
            'take_profit': float(t['take_profit']) if t['take_profit'] else None,
            'trade_type' : t['trade_type'],
            'symbol'     : t['symbol'],
            'lot_size'   : float(t['lot_size']),
            'time'       : t['opened_at'].isoformat(),
            'exit_price' : float(t['exit_price']) if t['exit_price'] else None,
        })

    context = {
        'account': account,
        'account_id': account.id,
        'trades': trades,
        'open_trades_json': json.dumps(formatted_trades, cls=DjangoJSONEncoder),
        'active_section': 'trading',
    }
    return render(request, 'simulator/dashboard.html', context)


# -----------------------
# API AJAX PARA ÓRDENES
# -----------------------
@csrf_exempt
@login_required
def api_orden(request):
    if request.method == 'POST':
        data = json.loads(request.body)

        side        = data.get('tipo', 'BUY').upper()
        symbol      = data.get('symbol', 'EUR/USD')
        lot_size    = Decimal(data.get('lot_size', '0.01'))
        stop_loss   = Decimal(data['stop_loss']) if data.get('stop_loss') else None
        take_profit = Decimal(data['take_profit']) if data.get('take_profit') else None

        acc_id = request.session.get("account_id")
        account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
        if not account:
            return JsonResponse({'error': 'No hay cuenta activa'}, status=403)

        entry_price = apply_spread_and_slippage(get_base_price(symbol), side)

        pos = Position.objects.create(
            account=account,
            symbol=symbol,
            side=side,
            qty=lot_size,
            avg_price=entry_price,
            sl=stop_loss,
            tp=take_profit,
        )

        closed = False
        pnl = Decimal('0.00')
        exit_price = None

        if stop_loss and ((side == "BUY" and entry_price <= stop_loss) or (side == "SELL" and entry_price >= stop_loss)):
            exit_price = stop_loss
            closed = True
        elif take_profit and ((side == "BUY" and entry_price >= take_profit) or (side == "SELL" and entry_price <= take_profit)):
            exit_price = take_profit
            closed = True

        if closed:
            direction = Decimal('1') if side == "BUY" else Decimal('-1')
            pnl = (exit_price - entry_price) * Decimal('100000') * direction
            pnl = pnl.quantize(Decimal('0.01'))

            Trade.objects.create(
                account=account,
                symbol=symbol,
                trade_type=side,
                lot_size=lot_size,
                entry_price=entry_price,
                exit_price=exit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                profit_loss=pnl,
                opened_at=pos.opened_at,
                closed_at=timezone.now(),
            )

            new_balance = account.balance + pnl
            LedgerEntry.objects.create(
                account=account,
                event_type=LedgerEntry.EV_REALIZED,
                amount=pnl,
                balance_after=new_balance,
                meta={"symbol": symbol, "side": side, "lots": str(lot_size)}
            )
            account.balance = new_balance
            account.equity = new_balance
            account.save()
            pos.delete()

            return JsonResponse({'ok': True, 'closed': True, 'pnl': float(pnl)})

        return JsonResponse({'ok': True, 'closed': False, 'entry_price': float(entry_price)})

    return JsonResponse({'error': 'Método no permitido'}, status=405)


# -----------------------
# HOME DASHBOARD
# -----------------------
@login_required
def switch_account_view(request, account_id):
    """Set session account_id and return to the page that triggered the switch."""
    acc = TradingAccount.objects.filter(pk=account_id, user=request.user).first()
    if acc:
        request.session['account_id'] = acc.id
    next_url = request.GET.get('next', '')
    return redirect(next_url or 'simulator:home')


def _resolve_account(request):
    """
    Return the TradingAccount for this session, falling back to the user's
    most-recent active account.  Updates session if a fallback is used.
    Returns None only when the user has no accounts at all.
    """
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        account = (
            TradingAccount.objects
            .filter(user=request.user, status='Activo')
            .order_by('-created_at')
            .first()
        )
        if account:
            request.session['account_id'] = account.id
    return account


@login_required
def home_view(request):
    account = _resolve_account(request)
    if not account:
        return redirect("simulator:login")

    all_accounts = (
        TradingAccount.objects
        .filter(user=request.user)
        .order_by('-created_at')
    )

    today = timezone.now().date()

    pnl_today = LedgerEntry.objects.filter(
        account=account,
        event_type=LedgerEntry.EV_REALIZED,
        created_at__date=today,
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    open_positions = Position.objects.filter(account=account)
    leverage = int(getattr(account, 'leverage', 50) or 50)
    margin_used = sum(
        float(p.qty) * float(p.avg_price) / leverage
        for p in open_positions
    )

    initial_balance = float(account.initial_balance or account.balance or 1)
    daily_loss = float(pnl_today) if float(pnl_today) < 0 else 0.0
    daily_dd_pct = round(abs(daily_loss) / initial_balance * 100, 1) if initial_balance else 0.0
    daily_dd_pct = min(daily_dd_pct, 100)

    total_trades = Trade.objects.filter(account=account).count()
    recent_moves = LedgerEntry.objects.filter(account=account).order_by('-created_at')[:8]
    recent_trades = Trade.objects.filter(account=account).order_by('-opened_at')[:5]

    return render(request, 'simulator/home.html', {
        'account': account,
        'all_accounts': all_accounts,
        'pnl_today': pnl_today,
        'margin_used': round(margin_used, 2),
        'open_positions_count': open_positions.count(),
        'daily_dd_pct': daily_dd_pct,
        'total_trades': total_trades,
        'recent_moves': recent_moves,
        'recent_trades': recent_trades,
        'active_section': 'dashboard',
    })


# HISTORIAL DE TRADES
# -----------------------
@login_required
def history_view(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        return redirect("simulator:login")

    trades = Trade.objects.filter(account=account).order_by('-opened_at')
    ledger = LedgerEntry.objects.filter(account=account).order_by('-created_at')[:50]

    total = trades.count()
    wins = trades.filter(profit_loss__gt=0).count()
    losses = trades.filter(profit_loss__lte=0).count()
    win_rate = round((wins / total * 100), 1) if total else 0

    agg = trades.aggregate(
        total_pnl=Sum('profit_loss'),
        best=Max('profit_loss'),
        worst=Min('profit_loss'),
    )
    total_pnl = agg['total_pnl'] or Decimal('0.00')
    best_trade = agg['best'] or Decimal('0.00')
    worst_trade = agg['worst'] or Decimal('0.00')

    context = {
        'account': account,
        'trades': trades,
        'ledger': ledger,
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'active_section': 'analysis',
    }
    return render(request, 'simulator/history.html', context)


# -----------------------
# DASHBOARD LIMPIO
# -----------------------
def clean_dashboard(request):
    return render(request, 'simulator/dashboard_clean.html')


# ═══════════════════════════════════════════════════════
# MY ACCOUNTS  —  User Capital Flow
# ═══════════════════════════════════════════════════════

@login_required
def accounts_view(request):
    """My Accounts page — wallet summary + all trading accounts."""
    # Pop flash messages so they're consumed and won't re-display on refresh
    acct_success = request.session.pop("acct_success", None)
    acct_error   = request.session.pop("acct_error",   None)

    wallet, _ = get_or_create_wallet(request.user)
    accounts  = (
        TradingAccount.objects
        .filter(user=request.user)
        .prefetch_related("positions")
        .order_by("-created_at")
    )
    accounts_data = []
    for acc in accounts:
        pos_count = Position.objects.filter(account=acc).count()
        accounts_data.append({"account": acc, "open_positions": pos_count})

    return render(request, "simulator/accounts.html", {
        "wallet":        wallet,
        "accounts_data": accounts_data,
        "create_form":   CreateAccountForm(),
        "active_section": "accounts",
        "acct_success":  acct_success,
        "acct_error":    acct_error,
    })


@login_required
def create_account_view(request):
    """POST: create a new trading account and optionally fund it from wallet."""
    if request.method != "POST":
        return redirect("simulator:accounts")

    form = CreateAccountForm(request.POST)
    if not form.is_valid():
        errors = " ".join(
            f"{f}: {', '.join(e)}" for f, e in form.errors.items()
        )
        request.session["acct_error"] = errors
        return redirect("simulator:accounts")

    account_type    = form.cleaned_data["account_type"]
    initial_deposit = form.cleaned_data.get("initial_deposit") or Decimal("0")
    leverage        = int(form.cleaned_data["leverage"])
    is_demo         = account_type == "DEMO"

    wallet, _ = get_or_create_wallet(request.user)

    if not is_demo and initial_deposit > 0:
        if wallet.available_balance < initial_deposit:
            request.session["acct_error"] = (
                f"Insufficient wallet balance. Available: ${wallet.available_balance:.2f}, "
                f"requested: ${initial_deposit:.2f}. Please deposit funds first."
            )
            return redirect("simulator:accounts")

    try:
        with transaction.atomic():
            if is_demo:
                # Demo: virtual $10,000 — no wallet debit
                account = TradingAccount.objects.create(
                    user=request.user,
                    wallet=wallet,
                    account_type="DEMO",
                    leverage=leverage,
                    initial_balance=Decimal("10000"),
                )
            else:
                # Real account: start at $0 then fund via transfer
                account = TradingAccount.objects.create(
                    user=request.user,
                    wallet=wallet,
                    account_type=account_type,
                    leverage=leverage,
                    initial_balance=Decimal("0"),
                )
                if initial_deposit > 0:
                    transfer_to_account(
                        wallet.id, account.id, initial_deposit,
                        note=f"Initial funding — {account_type} #{account.id}",
                        initiated_by=request.user,
                    )
    except InsufficientFunds as exc:
        request.session["acct_error"] = str(exc)
        return redirect("simulator:accounts")
    except Exception as exc:
        logger.error("create_account_view error: %s", exc, exc_info=True)
        request.session["acct_error"] = "Account creation failed. Please try again."
        return redirect("simulator:accounts")

    request.session["acct_success"] = (
        f"{account_type} account #{account.id} created successfully."
    )
    return redirect("simulator:dashboard_account", account_id=account.id)


@login_required
def fund_account_view(request, account_id):
    """POST: transfer funds from wallet into an existing trading account."""
    if request.method != "POST":
        return redirect("simulator:accounts")

    account = TradingAccount.objects.filter(pk=account_id, user=request.user).first()
    if not account:
        return redirect("simulator:accounts")

    form = FundAccountForm(request.POST)
    if not form.is_valid():
        request.session["acct_error"] = "Invalid amount."
        return redirect("simulator:accounts")

    amount = form.cleaned_data["amount"]
    wallet, _ = get_or_create_wallet(request.user)

    try:
        transfer_to_account(
            wallet.id, account.id, amount,
            note=f"Fund account #{account.id}",
            initiated_by=request.user,
        )
        request.session["acct_success"] = f"${amount:.2f} transferred to account #{account.id}."
    except InsufficientFunds:
        request.session["acct_error"] = (
            f"Insufficient wallet balance. Available: ${wallet.available_balance:.2f}."
        )
    except Exception as exc:
        logger.error("fund_account_view error: %s", exc, exc_info=True)
        request.session["acct_error"] = "Transfer failed. Please try again."

    return redirect("simulator:accounts")


@login_required
def withdraw_account_view(request, account_id):
    """POST: transfer funds from trading account back to wallet."""
    if request.method != "POST":
        return redirect("simulator:accounts")

    account = TradingAccount.objects.filter(pk=account_id, user=request.user).first()
    if not account:
        return redirect("simulator:accounts")

    form = WithdrawAccountForm(request.POST)
    if not form.is_valid():
        request.session["acct_error"] = "Invalid amount."
        return redirect("simulator:accounts")

    amount = form.cleaned_data["amount"]
    wallet, _ = get_or_create_wallet(request.user)

    try:
        transfer_to_wallet(
            wallet.id, account.id, amount,
            note=f"Withdraw from account #{account.id}",
            initiated_by=request.user,
        )
        request.session["acct_success"] = f"${amount:.2f} withdrawn to wallet from account #{account.id}."
    except (InsufficientFunds, ValueError) as exc:
        request.session["acct_error"] = str(exc)
    except Exception as exc:
        logger.error("withdraw_account_view error: %s", exc, exc_info=True)
        request.session["acct_error"] = "Withdrawal failed. Please try again."

    return redirect("simulator:accounts")


# ===================================================
# NOWPAYMENTS — imported from service layer
# ===================================================
from . import nowpayments as _np


# ===================================================
# DEPÓSITOS
# ===================================================

@login_required
def deposit_view(request):
    """
    Show deposit form (GET) or create a NowPayments payment and redirect to
    the on-platform payment status page (POST).

    Wallet-first: no trading account needed. Funds land in Wallet.
    """
    wallet, _ = get_or_create_wallet(request.user)
    error = None

    if request.method == "POST":
        form = DepositForm(request.POST)
        if form.is_valid():
            amount_usd      = form.cleaned_data["amount_usd"]
            crypto_currency = form.cleaned_data["crypto_currency"]

            # ── Guard: validate our code maps to a real NP currency ──────────
            try:
                np_code = to_np_code(crypto_currency)
            except ValueError as exc:
                logger.error("[deposit] invalid currency key '%s': %s", crypto_currency, exc)
                error = f"Moneda no soportada: {crypto_currency}"
                form = DepositForm()
                return render(request, "simulator/deposit.html", {
                    "form": form, "wallet": wallet, "error": error,
                    "active_section": "deposit",
                })

            deposit = Deposit.objects.create(
                user=request.user,
                amount_usd=amount_usd,
                crypto_currency=crypto_currency,
                status=Deposit.STATUS_PENDING,
            )
            logger.info(
                "[deposit] row created deposit_id=%d user=%s amount_usd=%.2f currency=%s",
                deposit.id, request.user.username, float(amount_usd), crypto_currency,
            )
            try:
                callback_url = request.build_absolute_uri(
                    reverse("simulator:deposit_callback")
                )
                data = _np.create_payment(
                    amount_usd, crypto_currency, deposit.id, callback_url
                )
                from django.utils.dateparse import parse_datetime
                exp_raw = data.get("expiration_estimate_date")
                Deposit.objects.filter(pk=deposit.pk).update(
                    nowpayments_payment_id = str(data.get("payment_id", "")),
                    nowpayments_invoice_url= data.get("invoice_url", "") or "",
                    pay_address            = data.get("pay_address", "") or "",
                    pay_amount             = data.get("pay_amount"),
                    expires_at             = parse_datetime(exp_raw) if exp_raw else None,
                    status                 = data.get("payment_status", Deposit.STATUS_WAITING),
                )
                logger.info(
                    "[deposit] invoice created deposit_id=%d payment_id=%s → redirect",
                    deposit.id, data.get("payment_id", "?"),
                )
                return redirect("simulator:deposit_status", deposit_id=deposit.id)

            except Exception as exc:
                np_body = getattr(getattr(exc, "response", None), "text", "")[:500]
                logger.error(
                    "[deposit] FAILED deposit_id=%d %s: %s np=%s",
                    deposit.id, type(exc).__name__, exc, np_body, exc_info=True,
                )
                Deposit.objects.filter(pk=deposit.pk).update(status=Deposit.STATUS_FAILED)
                error = "No se pudo crear el pago. Por favor intenta más tarde."
    else:
        form = DepositForm()

    return render(request, "simulator/deposit.html", {
        "form":           form,
        "wallet":         wallet,
        "error":          error,
        "active_section": "deposit",
    })


@csrf_exempt
def deposit_callback(request):
    """
    IPN webhook from NowPayments → credit Wallet.

    Idempotency: Deposit.credited is committed atomically with credit_wallet()
    inside select_for_update(). Any duplicate/retry sees credited=True and exits
    immediately — double-credit is impossible regardless of retry frequency.

    Security: HMAC-SHA512 signature verified before ANY DB read.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    body = request.body
    sig  = request.headers.get("x-nowpayments-sig", "")

    logger.info(
        "[callback] IPN received body_len=%d sig=%s…",
        len(body), sig[:16] if sig else "(none)",
    )

    if not _np.verify_ipn_signature(body, sig):
        logger.warning("[callback] IPN REJECTED — signature invalid sig=%s…", sig[:16])
        return JsonResponse({"error": "Invalid signature"}, status=400)

    logger.info("[callback] IPN signature VERIFIED")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    payment_id     = str(data.get("payment_id", ""))
    payment_status = data.get("payment_status", "")
    order_id       = str(data.get("order_id", ""))
    logger.info(
        "[callback] payment_id=%s order_id=%s status=%s",
        payment_id, order_id, payment_status,
    )

    # Locate deposit: payment_id first, fallback to order_id (our Deposit pk)
    deposit = Deposit.objects.filter(nowpayments_payment_id=payment_id).first()
    if not deposit and order_id:
        try:
            deposit = Deposit.objects.filter(pk=int(order_id)).first()
        except (ValueError, TypeError):
            pass

    if not deposit:
        logger.warning(
            "[callback] deposit NOT FOUND payment_id=%s order_id=%s", payment_id, order_id,
        )
        return JsonResponse({"error": "Deposit not found"}, status=404)

    logger.info("[callback] matched deposit_id=%d user=%s", deposit.id, deposit.user_id)

    with transaction.atomic():
        # Two concurrent callbacks serialize here; the loser reads credited=True.
        deposit = Deposit.objects.select_for_update().get(pk=deposit.pk)

        # ── Idempotency gate ──────────────────────────────────────────────
        if deposit.credited:
            logger.info(
                "[callback] DUPLICATE skipped — deposit_id=%d already credited",
                deposit.id,
            )
            return JsonResponse({"ok": True, "idempotent": True})
        # ─────────────────────────────────────────────────────────────────

        update_fields = {"status": payment_status}
        if not deposit.nowpayments_payment_id and payment_id:
            update_fields["nowpayments_payment_id"] = payment_id

        actually_paid = data.get("actually_paid_amount") or data.get("outcome_amount")
        if actually_paid:
            update_fields["confirmed_amount_usd"] = Decimal(str(actually_paid))

        if payment_status in Deposit.CREDITED_STATUSES:
            wallet, _ = get_or_create_wallet(deposit.user)
            credit_wallet(
                wallet.id,
                deposit.amount_usd,
                WalletTransaction.TX_DEPOSIT,
                deposit=deposit,
                note=f"NowPayments #{payment_id} {deposit.crypto_currency.upper()}",
            )

            # Drain pending_balance if it was incremented during confirming
            if deposit.status == Deposit.STATUS_CONFIRMING:
                from django.db.models import F
                from .models import Wallet as WalletModel
                WalletModel.objects.filter(pk=wallet.pk).update(
                    pending_balance=F("pending_balance") - deposit.amount_usd
                )

            update_fields["credited"]     = True
            update_fields["credited_at"]  = timezone.now()
            update_fields["confirmed_at"] = timezone.now()

            logger.info(
                "[callback] WALLET CREDITED deposit_id=%d payment_id=%s "
                "amount_usd=%s currency=%s wallet_id=%d",
                deposit.id, payment_id,
                deposit.amount_usd, deposit.crypto_currency.upper(), wallet.id,
            )

        elif payment_status == Deposit.STATUS_CONFIRMING:
            # Funds on-chain but not yet final — show as pending in wallet
            from django.db.models import F
            from .models import Wallet as WalletModel
            wallet, _ = get_or_create_wallet(deposit.user)
            WalletModel.objects.filter(pk=wallet.pk).update(
                pending_balance=F("pending_balance") + deposit.amount_usd
            )
            logger.info(
                "[callback] status → confirming deposit_id=%d — pending_balance +%s",
                deposit.id, deposit.amount_usd,
            )

        else:
            logger.info(
                "[callback] status update deposit_id=%d %s → %s (no credit)",
                deposit.id, deposit.status, payment_status,
            )

        Deposit.objects.filter(pk=deposit.pk).update(**update_fields)

    return JsonResponse({"ok": True})


# ── Payment status page (on-platform) ────────────────────────────────────────

@login_required
def deposit_status_view(request, deposit_id):
    """
    Show the on-platform payment page for a specific deposit.
    Includes crypto address, amount, expiry, and live-polling status banner.
    """
    deposit = Deposit.objects.filter(pk=deposit_id, user=request.user).first()
    if not deposit:
        return redirect("simulator:deposit_history")

    wallet, _ = get_or_create_wallet(request.user)

    is_final = deposit.credited or deposit.status in (
        Deposit.STATUS_FAILED, Deposit.STATUS_EXPIRED, Deposit.STATUS_REFUNDED
    )

    return render(request, "simulator/deposit_status.html", {
        "deposit":        deposit,
        "wallet":         wallet,
        "is_final":       is_final,
        "active_section": "deposit",
    })


@login_required
def deposit_status_json(request, deposit_id):
    """
    Lightweight JSON endpoint polled by the payment status page every 5 s.
    Returns: status, credited, pay_address, pay_amount, wallet_balance
    """
    deposit = Deposit.objects.filter(pk=deposit_id, user=request.user).only(
        "status", "credited", "amount_usd", "pay_address", "pay_amount",
        "crypto_currency", "nowpayments_payment_id",
    ).first()
    if not deposit:
        return JsonResponse({"error": "not found"}, status=404)

    logger.debug(
        "[poll] deposit_id=%d user=%s status=%s credited=%s",
        deposit.id, request.user.username, deposit.status, deposit.credited,
    )

    # Refresh from NP if still active and has a payment_id
    if (
        not deposit.credited
        and deposit.nowpayments_payment_id
        and deposit.status not in (
            Deposit.STATUS_EXPIRED, Deposit.STATUS_FAILED, Deposit.STATUS_REFUNDED
        )
    ):
        try:
            np_data = _np.get_payment_status(deposit.nowpayments_payment_id)
            new_status = np_data.get("payment_status", deposit.status)
            if new_status != deposit.status:
                logger.info(
                    "[poll] status change deposit_id=%d %s → %s",
                    deposit.id, deposit.status, new_status,
                )
                Deposit.objects.filter(pk=deposit.pk).update(status=new_status)
                deposit.status = new_status
        except Exception as exc:
            logger.debug("[poll] NP fetch error deposit_id=%d: %s", deposit_id, exc)

    wallet, _ = get_or_create_wallet(request.user)

    return JsonResponse({
        "status":          deposit.status,
        "credited":        deposit.credited,
        "amount_usd":      str(deposit.amount_usd),
        "pay_address":     deposit.pay_address or "",
        "pay_amount":      str(deposit.pay_amount) if deposit.pay_amount else "",
        "crypto_currency": deposit.crypto_currency.upper(),
        "wallet_balance":  str(wallet.available_balance),
    })


@login_required
def deposit_history_view(request):
    wallet, _ = get_or_create_wallet(request.user)
    deposits  = Deposit.objects.filter(user=request.user)
    return render(request, "simulator/deposit_history.html", {
        "wallet":         wallet,
        "deposits":       deposits,
        "active_section": "deposit_history",
    })


@login_required
def wallet_balance_json(request):
    wallet, _ = get_or_create_wallet(request.user)
    pending = Deposit.objects.filter(
        user=request.user, credited=False
    ).exclude(
        status__in=["failed", "expired", "refunded"]
    ).aggregate(total=Sum("amount_usd"))["total"] or Decimal("0")
    return JsonResponse({
        "balance": str(wallet.available_balance),
        "pending": str(pending),
    })


@login_required
def np_diagnostics_view(request):
    """
    Staff-only: verify API key, connectivity, and currency code mapping.
    GET /api/np-check/
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "forbidden"}, status=403)
    result = _np.api_status()
    logger.info("[NP-diag] %s", json.dumps(result, indent=2))
    return JsonResponse(result, json_dumps_params={"indent": 2})


@login_required
def np_supported_currencies_view(request):
    """
    Returns our CURRENCY_MAP with each code's NP support status.
    Uses the 30-min cached NP currencies list.
    GET /api/np-supported-currencies/
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "forbidden"}, status=403)

    force = request.GET.get("refresh") == "1"
    try:
        live = set(_np.get_available_currencies(force_refresh=force))
        status_map = {
            db_key: {
                "np_code":   np_code,
                "label":     label,
                "supported": np_code in live,
            }
            for db_key, (np_code, label) in CURRENCY_MAP.items()
        }
        return JsonResponse({
            "our_currencies": status_map,
            "total_np_currencies": len(live),
            "cache_ttl_seconds": _np._NP_CURRENCIES_CACHE_TTL,
        }, json_dumps_params={"indent": 2})
    except Exception as exc:
        logger.error("[NP] np_supported_currencies_view error: %s", exc)
        return JsonResponse({"error": str(exc)}, status=502)

# ── Crypto Withdrawal ─────────────────────────────────────────────────────────

@login_required
def withdraw_view(request):
    """
    GET  /withdraw/  — show withdrawal form with wallet balance.
    POST /withdraw/  — validate, debit wallet atomically, create WithdrawalRequest.
    Funds are reserved immediately; admin approval triggers the NP payout.
    """
    wallet, _ = get_or_create_wallet(request.user)
    error = None

    if request.method == "POST":
        form = WithdrawForm(request.POST)
        if form.is_valid():
            amount_usd      = form.cleaned_data["amount_usd"]
            crypto_currency = form.cleaned_data["crypto_currency"]
            wallet_address  = form.cleaned_data["wallet_address"]

            if wallet.available_balance < amount_usd:
                error = f"Balance insuficiente. Disponible: ${wallet.available_balance:,.2f}"
            else:
                try:
                    with transaction.atomic():
                        debit_tx = debit_wallet(
                            wallet.id,
                            amount_usd,
                            WalletTransaction.TX_WITHDRAW,
                            note=f"Retiro #{crypto_currency.upper()} → {wallet_address[:16]}… (pendiente)",
                            initiated_by=request.user,
                        )
                        wr = WithdrawalRequest.objects.create(
                            user=request.user,
                            amount_usd=amount_usd,
                            crypto_currency=crypto_currency,
                            wallet_address=wallet_address,
                            status=WithdrawalRequest.STATUS_PENDING,
                            debit_tx=debit_tx,
                        )

                    logger.info(
                        "[withdraw] created wr_id=%d user=%s amount_usd=%s currency=%s",
                        wr.id, request.user.username, amount_usd, crypto_currency,
                    )

                    try:
                        from django.core.mail import send_mail as _send_mail
                        _send_mail(
                            subject=f"Solicitud de retiro #{wr.id} recibida — Money Brokers",
                            message=(
                                f"Hola {request.user.username},\n\n"
                                f"Tu solicitud de retiro #{wr.id} fue recibida y está siendo revisada.\n\n"
                                f"  Monto:        ${amount_usd} USD\n"
                                f"  Criptomoneda: {crypto_currency.upper()}\n"
                                f"  Dirección:    {wallet_address}\n\n"
                                f"Te notificaremos cuando sea procesada (24-48h).\n\n"
                                f"— Money Brokers"
                            ),
                            from_email=settings.DEFAULT_FROM_EMAIL,
                            recipient_list=[request.user.email],
                            fail_silently=True,
                        )
                    except Exception as mail_exc:
                        logger.warning("[withdraw] confirmation email failed: %s", mail_exc)

                    return redirect("simulator:withdraw_history")

                except InsufficientFunds:
                    error = "Balance insuficiente."
                except Exception as exc:
                    logger.error("[withdraw] failed user=%s: %s", request.user.username, exc, exc_info=True)
                    error = "Error al procesar la solicitud. Intenta de nuevo."
    else:
        form = WithdrawForm()

    wallet.refresh_from_db()
    return render(request, "simulator/withdraw.html", {
        "form":           form,
        "wallet":         wallet,
        "error":          error,
        "active_section": "withdraw",
    })


@login_required
def withdraw_history_view(request):
    wallet, _   = get_or_create_wallet(request.user)
    withdrawals = WithdrawalRequest.objects.filter(user=request.user)
    return render(request, "simulator/withdraw_history.html", {
        "wallet":         wallet,
        "withdrawals":    withdrawals,
        "active_section": "withdraw_history",
    })


@csrf_exempt
def withdraw_payout_callback(request):
    """
    IPN webhook from NowPayments for payout status updates.
    POST /withdraw/callback/

    NP payout IPN body:
      { "id": batch_id, "status": "...", "withdrawals": [{id, status, ...}] }

    On FINISHED → mark completed.
    On FAILED   → refund wallet via TX_CORRECTION.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    body = request.body
    sig  = request.headers.get("x-nowpayments-sig", "")
    logger.info("[payout_cb] IPN received body_len=%d sig=%s…", len(body), sig[:16] if sig else "(none)")

    if not _np.verify_ipn_signature(body, sig):
        logger.warning("[payout_cb] IPN REJECTED — signature invalid")
        return JsonResponse({"error": "Invalid signature"}, status=400)

    logger.info("[payout_cb] IPN signature VERIFIED")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    batch_id      = str(data.get("id", ""))
    batch_status  = data.get("status", "")
    withdrawals_d = data.get("withdrawals", [])
    logger.info("[payout_cb] batch_id=%s status=%s items=%d", batch_id, batch_status, len(withdrawals_d))

    _NP_TO_STATUS = {
        "FINISHED": WithdrawalRequest.STATUS_COMPLETED,
        "FAILED":   WithdrawalRequest.STATUS_FAILED,
        "ROLLING":  WithdrawalRequest.STATUS_PROCESSING,
        "CREATED":  WithdrawalRequest.STATUS_PROCESSING,
    }

    for wd in withdrawals_d:
        payout_id  = str(wd.get("id", ""))
        np_status  = str(wd.get("status", "")).upper()
        new_status = _NP_TO_STATUS.get(np_status)
        if not new_status:
            continue

        wr = WithdrawalRequest.objects.filter(np_payout_id=payout_id).first()
        if not wr and batch_id:
            wr = WithdrawalRequest.objects.filter(np_batch_id=batch_id).first()
        if not wr:
            logger.warning("[payout_cb] no withdrawal found payout_id=%s batch_id=%s", payout_id, batch_id)
            continue

        with transaction.atomic():
            wr = WithdrawalRequest.objects.select_for_update().get(pk=wr.pk)
            if wr.status in (WithdrawalRequest.STATUS_COMPLETED, WithdrawalRequest.STATUS_REJECTED):
                logger.info("[payout_cb] already final wr_id=%d status=%s — skip", wr.id, wr.status)
                continue

            update = {"status": new_status, "np_payout_status": wd.get("status", "")}
            if payout_id and not wr.np_payout_id:
                update["np_payout_id"] = payout_id

            if new_status == WithdrawalRequest.STATUS_FAILED:
                wallet, _ = get_or_create_wallet(wr.user)
                credit_wallet(
                    wallet.id,
                    wr.amount_usd,
                    WalletTransaction.TX_CORRECTION,
                    note=f"Refund — payout #{payout_id} failed",
                )
                logger.info("[payout_cb] REFUNDED wr_id=%d amount=%s", wr.id, wr.amount_usd)
                try:
                    from django.core.mail import send_mail as _send_mail
                    _send_mail(
                        subject=f"Retiro #{wr.id} fallido — fondos devueltos",
                        message=(
                            f"Hola {wr.user.username},\n\n"
                            f"El pago de tu retiro #{wr.id} no pudo completarse.\n"
                            f"${wr.amount_usd} USD fueron devueltos a tu wallet automáticamente.\n\n"
                            f"Contacta a soporte si tienes dudas.\n\n"
                            f"— Money Brokers"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[wr.user.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            elif new_status == WithdrawalRequest.STATUS_COMPLETED:
                logger.info("[payout_cb] COMPLETED wr_id=%d payout_id=%s", wr.id, payout_id)
                try:
                    from django.core.mail import send_mail as _send_mail
                    _send_mail(
                        subject=f"Retiro #{wr.id} completado — Money Brokers",
                        message=(
                            f"Hola {wr.user.username},\n\n"
                            f"Tu retiro #{wr.id} fue enviado exitosamente.\n\n"
                            f"  Monto:     ${wr.amount_usd} USD\n"
                            f"  Cripto:    {wr.crypto_amount} {wr.crypto_currency.upper()}\n"
                            f"  Dirección: {wr.wallet_address}\n\n"
                            f"— Money Brokers"
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[wr.user.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            WithdrawalRequest.objects.filter(pk=wr.pk).update(**update)

    return JsonResponse({"ok": True})


# ──────────────────────────────────────────────────────────────
# Health check — Redis + DB + Channels layer
# GET /api/health/  →  200 OK   {"status":"ok", ...}
#                   →  503      {"status":"degraded", ...}
# ──────────────────────────────────────────────────────────────
def health_check(request):
    results = {}
    ok = True

    # ── Database ──
    try:
        from django.db import connection
        t0 = time.monotonic()
        connection.ensure_connection()
        results["db"] = {"status": "ok", "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as exc:
        results["db"] = {"status": "error", "detail": str(exc)}
        ok = False

    # ── Redis / Channel layer ──
    try:
        import redis as redis_lib
        redis_url = getattr(settings, "REDIS_URL", "") or ""
        if redis_url:
            t0 = time.monotonic()
            r = redis_lib.from_url(redis_url, socket_connect_timeout=2)
            r.ping()
            results["redis"] = {"status": "ok", "ms": round((time.monotonic() - t0) * 1000, 1)}
            results["channel_layer"] = "redis"
        else:
            results["redis"] = {"status": "not_configured"}
            results["channel_layer"] = "in_memory"
    except Exception as exc:
        results["redis"] = {"status": "error", "detail": str(exc)}
        results["channel_layer"] = "error"
        ok = False

    payload = {"status": "ok" if ok else "degraded", **results}
    return JsonResponse(payload, status=200 if ok else 503)


# ──────────────────────────────────────────────────────────────
# Operational metrics — staff-only
# GET /api/metrics/  →  200 {"status":"ok", accounts:{}, redis:{}, celery:{}, ...}
#                    →  503 if any subsystem is degraded
#                    →  403 if not staff
# ──────────────────────────────────────────────────────────────
def metrics_view(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return JsonResponse({"error": "forbidden"}, status=403)

    import time as _t
    from datetime import timedelta

    t_start = _t.monotonic()
    result: dict = {"ts": _t.time(), "status": "ok"}
    degraded = False

    # ── DB counters ───────────────────────────────────────────────────────────
    try:
        result["accounts"] = {
            "total": TradingAccount.objects.count(),
            "active": TradingAccount.objects.filter(status="Activo").count(),
        }
        from .models import Deposit, WithdrawalRequest
        result["positions"] = {
            "open": Position.objects.count(),
        }
        result["deposits"] = {
            "pending": Deposit.objects.filter(credited=False).count(),
            "credited_24h": Deposit.objects.filter(
                credited=True,
                created_at__gte=timezone.now() - timedelta(hours=24),
            ).count(),
        }
        result["withdrawals"] = {
            "pending": WithdrawalRequest.objects.filter(status="pending").count(),
            "processing": WithdrawalRequest.objects.filter(status="processing").count(),
        }
    except Exception as exc:
        result["db_metrics"] = {"error": str(exc)}
        degraded = True

    # ── Redis stats + WS counter + failure ring buffer ────────────────────────
    try:
        import redis as redis_lib
        redis_url = getattr(settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        t0 = _t.monotonic()
        r = redis_lib.from_url(redis_url, socket_connect_timeout=2)
        r.ping()
        info = r.info()
        ws_count = int(r.get("trx:metrics:ws_connections") or 0)
        raw_failures = r.lrange("trx:metrics:task_failures", 0, 9)
        failure_total = r.llen("trx:metrics:task_failures")
        result["redis"] = {
            "status": "ok",
            "ping_ms": round((_t.monotonic() - t0) * 1000, 1),
            "memory_mb": round(info.get("used_memory", 0) / 1_048_576, 2),
            "peak_memory_mb": round(info.get("used_memory_peak", 0) / 1_048_576, 2),
            "connected_clients": info.get("connected_clients", 0),
            "uptime_days": info.get("uptime_in_days", 0),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
        }
        result["websockets"] = {"active_connections": max(0, ws_count)}
        result["task_failures"] = {
            "stored_total": failure_total,
            "last_10": [json.loads(e) for e in raw_failures],
        }
    except Exception as exc:
        result["redis"] = {"status": "error", "detail": str(exc)}
        degraded = True

    # ── Celery worker inspection ──────────────────────────────────────────────
    # Uses a single ping broadcast (fast) — avoids 3 × timeout serial calls.
    try:
        from trx_simulator.celery import app as celery_app
        insp = celery_app.control.inspect(timeout=1.0)
        ping_resp = insp.ping() or {}
        workers   = list(ping_resp.keys())
        active    = insp.active()   or {} if workers else {}
        active_tasks = sum(len(v) for v in active.values()) if workers else 0
        result["celery"] = {
            "workers_online": len(workers),
            "worker_names": workers,
            "active_tasks": active_tasks,
        }
        if not workers:
            result["celery"]["warning"] = "no workers online"
    except Exception as exc:
        result["celery"] = {"error": str(exc)}
        degraded = True

    result["elapsed_ms"] = round((_t.monotonic() - t_start) * 1000, 1)
    result["status"] = "degraded" if degraded else "ok"
    return JsonResponse(result, status=200 if not degraded else 503)


# ──────────────────────────────────────────────────────────────
# Broker monitoring — staff-only operational dashboard
# GET /api/broker/monitoring/  →  200 {"alerts":{}, "pnl":{}, "exposure":{}, ...}
#                              →  503 if any section failed
#                              →  403 if not staff
# ──────────────────────────────────────────────────────────────
def broker_monitoring_view(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return JsonResponse({"error": "forbidden"}, status=403)

    from .broker_monitoring import full_report
    report = full_report()
    has_errors = bool(report.get("errors"))
    return JsonResponse(report, status=503 if has_errors else 200)


# ──────────────────────────────────────────────────────────────
# Equity snapshots — staff-only time-series query
#
# GET /api/broker/snapshots/?type=broker
# GET /api/broker/snapshots/?type=account&account_id=<id>
#
# Optional: &since=<ISO8601>&until=<ISO8601>&limit=<1-10080>
# Defaults:  since = now-24h,  until = now,  limit = 1440
# ──────────────────────────────────────────────────────────────
def snapshots_view(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return JsonResponse({"error": "forbidden"}, status=403)

    from django.utils.dateparse import parse_datetime
    from .snapshots import query_broker_snapshots, query_account_snapshots

    snap_type  = request.GET.get("type", "broker")
    limit      = min(max(int(request.GET.get("limit", 1440)), 1), 10080)
    now        = timezone.now()
    default_since = now - timezone.timedelta(hours=24)

    raw_since = request.GET.get("since", "")
    raw_until = request.GET.get("until", "")
    since = parse_datetime(raw_since) if raw_since else default_since
    until = parse_datetime(raw_until) if raw_until else now
    if since is None or until is None:
        return JsonResponse({"error": "invalid since/until — use ISO 8601"}, status=400)
    if since >= until:
        return JsonResponse({"error": "since must be before until"}, status=400)

    if snap_type == "broker":
        data = query_broker_snapshots(since, until, limit)
        return JsonResponse({
            "type": "broker", "since": since.isoformat(), "until": until.isoformat(),
            "count": len(data), "limit": limit, "data": data,
        })

    elif snap_type == "account":
        try:
            account_id = int(request.GET.get("account_id", ""))
        except (TypeError, ValueError):
            return JsonResponse({"error": "account_id is required for type=account"}, status=400)
        data = query_account_snapshots(account_id, since, until, limit)
        return JsonResponse({
            "type": "account", "account_id": account_id,
            "since": since.isoformat(), "until": until.isoformat(),
            "count": len(data), "limit": limit, "data": data,
        })

    return JsonResponse({"error": "type must be 'broker' or 'account'"}, status=400)
