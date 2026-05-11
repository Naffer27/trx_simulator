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
from django.urls import reverse
import json, random, hmac, hashlib, logging
import requests as http_requests

from .models import Purchase, TradingAccount, Trade, Position, LedgerEntry, Deposit
from .forms import LoginForm, RegisterForm, DepositForm

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
                # Elegir tier/balance desde Purchase si existe, si no por defecto/última cuenta
                existing_acc = TradingAccount.objects.filter(user=user).first()
                tier = (purchase.tier if purchase else (existing_acc.tier if existing_acc else "10K"))
                base_balance = 10000 if tier == "10K" else (50000 if tier == "50K" else 100000)

                account, created = TradingAccount.objects.get_or_create(
                    user=user,
                    defaults={
                        'tier': tier,
                        'phase': 'Fase 1',
                        'balance': base_balance,
                        'equity': base_balance,
                        'status': "Activo"
                    }
                )

                request.session['account_id'] = account.id
                auth_login(request, user)
                return redirect('simulator:home')

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
def trading_dashboard(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()

    if not account:
        return redirect("simulator:login")

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
            pnl = (exit_price - entry_price) * Decimal('100000') * direction
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
        'trades': trades,
        'open_trades_json': json.dumps(formatted_trades, cls=DjangoJSONEncoder),
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
def home_view(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        return redirect("simulator:login")

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

    initial_balance = float({
        '10K': 10000, '50K': 50000, '100K': 100000,
    }.get(account.tier, float(account.balance)))
    daily_loss = float(pnl_today) if float(pnl_today) < 0 else 0.0
    daily_dd_pct = round(abs(daily_loss) / initial_balance * 100, 1) if initial_balance else 0.0
    daily_dd_pct = min(daily_dd_pct, 100)

    total_trades = Trade.objects.filter(account=account).count()
    recent_moves = LedgerEntry.objects.filter(account=account).order_by('-created_at')[:8]
    recent_trades = Trade.objects.filter(account=account).order_by('-opened_at')[:5]

    return render(request, 'simulator/home.html', {
        'account': account,
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


# ===================================================
# NOWPAYMENTS — helpers privados
# ===================================================

def _nowpayments_create_payment(amount_usd, pay_currency, deposit_id, callback_url):
    resp = http_requests.post(
        "https://api.nowpayments.io/v1/payment",
        headers={
            "x-api-key": getattr(settings, "NOWPAYMENTS_API_KEY", ""),
            "Content-Type": "application/json",
        },
        json={
            "price_amount": float(amount_usd),
            "price_currency": "usd",
            "pay_currency": pay_currency,
            "ipn_callback_url": callback_url,
            "order_id": str(deposit_id),
            "order_description": f"Money Brokers Deposit #{deposit_id}",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _verify_nowpayments_ipn(body_bytes, signature):
    secret = getattr(settings, "NOWPAYMENTS_IPN_SECRET", "")
    if not secret:
        return True  # dev: skip if secret not configured
    try:
        payload = json.loads(body_bytes)
        sorted_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        computed = hmac.new(
            secret.encode("utf-8"),
            sorted_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


# ===================================================
# DEPÓSITOS
# ===================================================

@login_required
def deposit_view(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        return redirect("simulator:login")

    error = None
    if request.method == "POST":
        form = DepositForm(request.POST)
        if form.is_valid():
            amount_usd = form.cleaned_data["amount_usd"]
            crypto_currency = form.cleaned_data["crypto_currency"]

            if not getattr(settings, "NOWPAYMENTS_API_KEY", ""):
                error = "Sistema de depósitos no configurado. Contacta al soporte."
            else:
                deposit = Deposit.objects.create(
                    user=request.user,
                    amount_usd=amount_usd,
                    crypto_currency=crypto_currency,
                    status=Deposit.STATUS_PENDING,
                )
                try:
                    callback_url = request.build_absolute_uri(
                        reverse("simulator:deposit_callback")
                    )
                    data = _nowpayments_create_payment(
                        amount_usd, crypto_currency, deposit.id, callback_url
                    )
                    deposit.nowpayments_payment_id = str(data.get("payment_id", ""))
                    deposit.nowpayments_invoice_url = data.get("invoice_url", "")
                    deposit.status = data.get("payment_status", Deposit.STATUS_WAITING)
                    deposit.save()

                    invoice_url = deposit.nowpayments_invoice_url
                    if invoice_url:
                        return redirect(invoice_url)
                    return redirect("simulator:deposit_history")
                except Exception as exc:
                    logger.error("NowPayments create payment error: %s", exc)
                    deposit.status = Deposit.STATUS_FAILED
                    deposit.save()
                    error = "Error al crear el pago. Por favor intenta más tarde."
    else:
        form = DepositForm()

    return render(request, "simulator/deposit.html", {
        "form": form,
        "account": account,
        "error": error,
        "active_section": "deposit",
    })


@csrf_exempt
def deposit_callback(request):
    """IPN webhook from NowPayments."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    body = request.body
    sig = request.headers.get("x-nowpayments-sig", "")

    if not _verify_nowpayments_ipn(body, sig):
        logger.warning("Invalid NowPayments IPN signature")
        return JsonResponse({"error": "Invalid signature"}, status=400)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    payment_id = str(data.get("payment_id", ""))
    payment_status = data.get("payment_status", "")
    order_id = str(data.get("order_id", ""))

    deposit = Deposit.objects.filter(nowpayments_payment_id=payment_id).first()
    if not deposit and order_id:
        try:
            deposit = Deposit.objects.filter(pk=int(order_id)).first()
        except (ValueError, TypeError):
            pass

    if not deposit:
        logger.warning("Deposit not found for payment_id=%s order_id=%s", payment_id, order_id)
        return JsonResponse({"error": "Deposit not found"}, status=404)

    old_status = deposit.status
    deposit.status = payment_status
    if not deposit.nowpayments_payment_id and payment_id:
        deposit.nowpayments_payment_id = payment_id

    if payment_status in Deposit.CREDITED_STATUSES and old_status not in Deposit.CREDITED_STATUSES:
        deposit.confirmed_at = timezone.now()
        account = TradingAccount.objects.filter(user=deposit.user).first()
        if account:
            new_balance = account.balance + deposit.amount_usd
            new_equity = account.equity + deposit.amount_usd
            LedgerEntry.objects.create(
                account=account,
                event_type=LedgerEntry.EV_DEPOSIT,
                amount=deposit.amount_usd,
                balance_after=new_balance,
                meta={
                    "source": "nowpayments",
                    "payment_id": payment_id,
                    "crypto": deposit.crypto_currency,
                    "deposit_id": deposit.id,
                },
            )
            account.balance = new_balance
            account.equity = new_equity
            account.save(update_fields=["balance", "equity"])
            logger.info(
                "Credited $%s to account #%s (deposit #%s)",
                deposit.amount_usd, account.id, deposit.id,
            )

    deposit.save()
    return JsonResponse({"ok": True})


@login_required
def deposit_history_view(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        return redirect("simulator:login")

    deposits = Deposit.objects.filter(user=request.user)
    return render(request, "simulator/deposit_history.html", {
        "account": account,
        "deposits": deposits,
        "active_section": "deposit",
    })