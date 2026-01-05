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
from django.conf import settings  # ✅ para BROKER_ACCESS_CODE / DEBUG
import json, random

from .models import Purchase, TradingAccount, Trade, Position, LedgerEntry
from .forms import LoginForm, RegisterForm


# ===== Configuración de mercado (simulada) =====
DEFAULT_PRICE = Decimal('1.10000')
SPREAD = Decimal('0.00020')      # Spread fijo
SLIPPAGE = Decimal('0.00010')    # Slippage máx


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
                return redirect('simulator:dashboard')

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

        entry_price = apply_spread_and_slippage(DEFAULT_PRICE, trade_type)

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

        entry_price = apply_spread_and_slippage(DEFAULT_PRICE, side)

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
# DASHBOARD LIMPIO
# -----------------------
def clean_dashboard(request):
    return render(request, 'simulator/dashboard_clean.html')