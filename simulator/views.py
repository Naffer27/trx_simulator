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
import json, random, logging, time, secrets as _secrets

from .models import (
    Purchase, TradingAccount, Trade, Position, LedgerEntry, Deposit,
    WalletTransaction, WithdrawalRequest, MARGIN_ENGINE_TYPES,
    CalendarEvent, Referral, Bonus, BrokerDocument, ExpertAdvisor,
    TradingViolation, TraderScore, AccountEquitySnapshot,
    ChallengeEnrollment, ChallengeProduct, AccountProduct, Wallet,
    EmailVerification, TermsAcceptance, TERMS_VERSION, RISK_DISCLOSURE_VERSION,
    KYCProfile, SupportTicket,
)
from .challenge_engine import (
    evaluate_phase as _ce_evaluate_phase,
    IN_PROGRESS as _CE_IN_PROGRESS,
    PASSED as _CE_PASSED,
    FAILED as _CE_FAILED,
    activate_challenge_enrollment as _ce_activate,
)
from .forms import LoginForm, RegisterForm, DepositForm, WithdrawForm, CreateAccountForm, FundAccountForm, WithdrawAccountForm, KYCProfileForm, UserProfileForm
from .wallet_ledger import credit_wallet, debit_wallet, transfer_to_account, transfer_to_wallet, get_or_create_wallet, InsufficientFunds
from .currencies import to_np_code, CURRENCY_MAP
from .observability import security_log, get_client_ip
from .ratelimit import rate_limit
from .audit import (
    log_audit,
    EV_AUTH_LOGIN_SUCCESS, EV_AUTH_LOGIN_FAILED,
    EV_DEPOSIT_CREATED, EV_DEPOSIT_CREDITED, EV_DEPOSIT_CALLBACK,
    EV_WITHDRAW_REQUEST, EV_WITHDRAW_CALLBACK, EV_WITHDRAW_APPROVED,
    EV_WITHDRAW_REJECTED, EV_WITHDRAW_COMPLETE,
    EV_WITHDRAW_FAILED, EV_WITHDRAW_REFUNDED,
    EV_ACCOUNT_FUNDED, EV_ACCOUNT_WITHDRAWN,
    EV_ADMIN_VIEW,
)


def _mask_wallet(addr: str) -> str:
    """Return a privacy-safe representation: first 6 + ... + last 4 chars."""
    if not addr or len(addr) <= 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def _is_email_verified(user) -> bool:
    """Return True only when the user has a confirmed EmailVerification record."""
    try:
        return user.email_verification.verified
    except Exception:
        return False


_EMAIL_GATE_MSG = (
    "Debes verificar tu email antes de usar funciones financieras. "
    "Revisa tu bandeja de entrada."
)


def _has_accepted_terms(user) -> bool:
    """Return True if the user has accepted the current terms and risk disclosure versions."""
    try:
        return TermsAcceptance.objects.filter(
            user=user,
            terms_version=TERMS_VERSION,
            risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
        ).exists()
    except Exception:
        return False


_TERMS_GATE_MSG = (
    "Debes aceptar los términos y el aviso de riesgo antes de usar funciones financieras."
)

_KYC_GATE_MSG = (
    "Debes completar y aprobar tu verificación KYC antes de retirar fondos."
)

_DAILY_LIMIT_STATUSES = (
    WithdrawalRequest.STATUS_PENDING,
    WithdrawalRequest.STATUS_PROCESSING,
    WithdrawalRequest.STATUS_APPROVED,
    WithdrawalRequest.STATUS_COMPLETED,
)


def _get_daily_withdrawal_used(user):
    """Return total USD used toward the daily limit (UTC day) for this user."""
    today = timezone.now().date()
    agg = WithdrawalRequest.objects.filter(
        user=user,
        created_at__date=today,
        status__in=_DAILY_LIMIT_STATUSES,
    ).aggregate(total=Sum("amount_usd"))
    return agg["total"] or Decimal("0")


from market_data.symbol_specs import get_spec as _get_sym_spec, allowed_symbols as _allowed_symbols

logger = logging.getLogger(__name__)


def landing_view(request):
    return render(request, 'simulator/landing.html')


# ===== Configuración de mercado (simulada) =====
# Prices come from the symbol registry — single source of truth.
SYMBOL_BASE_PRICES = {
    sym: Decimal(str(_get_sym_spec(sym).base_price))
    for sym in _allowed_symbols()
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
def register_view(request):
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()

            # Wallet con saldo cero
            Wallet.objects.get_or_create(user=user)

            # Email verification — registro sin cuenta verificada
            EmailVerification.objects.create(user=user, verified=False)
            try:
                from .email_verification import make_email_token as _make_token
                from .tasks import send_email_async as _send_async
                _token = _make_token(user.pk)
                _verify_url = (
                    settings.SITE_URL + reverse("simulator:verify_email", args=[_token])
                )
                _send_async.delay(
                    subject="Verifica tu email — TRX Simulator",
                    message=(
                        f"Hola {user.username},\n\n"
                        f"Verifica tu email visitando el siguiente enlace:\n{_verify_url}\n\n"
                        "El enlace expira en 48 horas.\n\n"
                        "Si no creaste esta cuenta, ignora este mensaje."
                    ),
                    recipient_list=[user.email],
                )
                _send_async.delay(
                    subject="📢 Nuevo usuario registrado",
                    message=(
                        f"Un nuevo usuario ha creado cuenta:\n\n"
                        f"- Usuario: {user.username}\n"
                        f"- Email: {user.email}\n"
                    ),
                    recipient_list=["nafferphotographer@gmail.com"],
                )
            except Exception as _exc:
                logger.warning("[register] email task failed for user=%s: %s",
                               user.username, _exc)

            auth_login(request, user)
            return redirect("simulator:accounts")
    else:
        form = RegisterForm()
    return render(request, "simulator/register.html", {"form": form})


# -----------------------
# LOGIN CON CÓDIGO DE ACCESO (flexible)
# -----------------------
@rate_limit("login", limit=8, window=300)
def login_view(request):
    error = None

    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')

        # Allow login with email address — resolve to username transparently
        if "@" in username:
            from django.contrib.auth import get_user_model as _gum
            _email_user = _gum().objects.filter(email=username).first()
            if _email_user:
                username = _email_user.username

        ip   = get_client_ip(request)
        user = authenticate(request, username=username, password=password)

        if user is None:
            error = "Usuario o contraseña inválidos"
            security_log("auth.login_failed", ip=ip, username=username, reason="bad_credentials")
            log_audit(request, EV_AUTH_LOGIN_FAILED,
                      f"Login failed (bad credentials) for {username}",
                      detail={"username": username, "ip": ip, "reason": "bad_credentials"})
        else:
            auth_login(request, user)
            security_log("auth.login_success", level="info", ip=ip,
                         username=username, user_id=user.pk)
            log_audit(request, EV_AUTH_LOGIN_SUCCESS,
                      f"Login success for {username}",
                      detail={"username": username, "ip": ip})
            active = (
                TradingAccount.objects
                .filter(user=user, status="Activo")
                .order_by("-id")
                .first()
            )
            if active:
                request.session["account_id"] = active.id
            return redirect("simulator:home")

    return render(request, 'simulator/login.html', {'error': error, 'form': LoginForm()})


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

    # ── Phase 4A: Trader Intelligence ────────────────────────────────────
    _tier_limits = {
        "10K":  {"max_dd": 10.0, "max_daily": 5.0},
        "50K":  {"max_dd":  8.0, "max_daily": 4.0},
        "100K": {"max_dd":  6.0, "max_daily": 3.0},
    }
    _tl = _tier_limits.get(account.tier, _tier_limits["10K"])
    intel_max_dd_pct    = _tl["max_dd"]
    intel_max_daily_pct = _tl["max_daily"]

    _peak = float(account.peak_balance or account.balance or 1)
    _bal  = float(account.balance or 0)
    realized_dd_pct = max(0.0, (_peak - _bal) / _peak * 100) if _peak > 0 else 0.0

    _today = timezone.now().date()
    _today_pnl_raw = LedgerEntry.objects.filter(
        account=account,
        event_type=LedgerEntry.EV_REALIZED,
        created_at__date=_today,
    ).aggregate(t=Sum("amount"))["t"]
    today_realized_pnl    = float(_today_pnl_raw or 0)
    daily_realized_dd_pct = (
        abs(today_realized_pnl) / _peak * 100
        if (_peak > 0 and today_realized_pnl < 0) else 0.0
    )

    _initial       = float(account.initial_balance or _peak)
    _profit_gained = _bal - _initial
    has_profit_target = (
        account.profit_target is not None and account.profit_target > 0
    )
    profit_pct = (
        max(0.0, min(100.0, _profit_gained / float(account.profit_target) * 100))
        if has_profit_target else None
    )

    equity_curve = list(
        AccountEquitySnapshot.objects.filter(account=account)
        .order_by("-taken_at")[:24]
        .values("taken_at", "equity", "balance")
    )
    equity_curve.reverse()
    equity_curve_json = json.dumps(
        [{"e": float(p["equity"]), "b": float(p["balance"])} for p in equity_curve]
    )

    try:
        trader_score = account.trader_score
    except Exception:
        trader_score = None

    recent_violations = list(
        TradingViolation.objects.filter(account=account).order_by("-created_at")[:5]
    )

    _trade_agg = Trade.objects.filter(
        account=account, profit_loss__isnull=False
    ).aggregate(
        total_trades=Count("id"),
        wins=Count("id", filter=Q(profit_loss__gt=0)),
        total_pnl=Sum("profit_loss"),
    )
    _total_trades      = _trade_agg["total_trades"] or 0
    win_rate_pct       = (_trade_agg["wins"] / _total_trades * 100) if _total_trades > 0 else None
    total_realized_pnl = float(_trade_agg["total_pnl"] or 0)

    # Bar fill percentages (capped 0-100) and safe flags for template
    daily_dd_bar_pct = (
        min(100.0, daily_realized_dd_pct / intel_max_daily_pct * 100)
        if intel_max_daily_pct > 0 else 0.0
    )
    max_dd_bar_pct = (
        min(100.0, realized_dd_pct / intel_max_dd_pct * 100)
        if intel_max_dd_pct > 0 else 0.0
    )
    daily_dd_safe = daily_realized_dd_pct < (intel_max_daily_pct * 0.5)
    max_dd_safe   = realized_dd_pct       < (intel_max_dd_pct   * 0.5)
    # ─────────────────────────────────────────────────────────────────────

    # ── Phase 4C.1: Challenge Lifecycle ──────────────────────────────────
    challenge_enrollment = None
    for _rel in ("enrollment_phase1", "enrollment_phase2", "enrollment_funded"):
        try:
            challenge_enrollment = getattr(account, _rel)
            break
        except ChallengeEnrollment.DoesNotExist:
            pass

    challenge_phase_label      = None
    challenge_eval_status      = None
    challenge_eval_fail_reason = None
    challenge_trading_days     = None
    challenge_min_trading_days = None
    challenge_days_remaining   = None
    challenge_max_duration_days = None
    challenge_funded_config    = None

    if challenge_enrollment:
        _enroll_product = challenge_enrollment.product
        _enroll_status  = challenge_enrollment.status

        if _enroll_status == ChallengeEnrollment.ST_PHASE_1:
            challenge_phase_label       = "Phase 1"
            challenge_min_trading_days  = _enroll_product.p1_min_trading_days
            challenge_max_duration_days = _enroll_product.p1_max_duration_days
        elif _enroll_status == ChallengeEnrollment.ST_PHASE_2:
            challenge_phase_label       = "Phase 2"
            challenge_min_trading_days  = _enroll_product.p2_min_trading_days
            challenge_max_duration_days = _enroll_product.p2_max_duration_days
        elif _enroll_status == ChallengeEnrollment.ST_FUNDED:
            challenge_phase_label = "Funded"
        elif _enroll_status == ChallengeEnrollment.ST_FAILED:
            challenge_phase_label = "Failed"
        else:
            challenge_phase_label = _enroll_status

        if _enroll_status in (ChallengeEnrollment.ST_PHASE_1, ChallengeEnrollment.ST_PHASE_2):
            _eval = _ce_evaluate_phase(challenge_enrollment)
            challenge_eval_status      = _eval.status
            challenge_eval_fail_reason = _eval.fail_reason
            _metrics = _eval.metrics
            challenge_trading_days  = _metrics.get("trading_days", 0)
            _days_elapsed           = _metrics.get("days_elapsed", 0)
            challenge_days_remaining = max(0, challenge_max_duration_days - _days_elapsed)

        if _enroll_status == ChallengeEnrollment.ST_FUNDED:
            try:
                challenge_funded_config = challenge_enrollment.funded_config
            except Exception:
                challenge_funded_config = None
    # ─────────────────────────────────────────────────────────────────────

    # ── Phase 4C.2: Funded Dashboard Section ─────────────────────────────
    funded_section           = False
    funded_cycle_profit      = None
    funded_trader_cut        = None
    funded_broker_cut        = None
    funded_payout_eligible   = None
    funded_payout_label      = None
    funded_trading_days      = None
    funded_min_trading_days  = None
    funded_next_payout_date  = None
    funded_days_until_payout = None

    _fc = challenge_funded_config
    if _fc is not None and account.account_type == "FUNDED":
        funded_section = True
        _ZERO_D = Decimal("0")
        _PENNY_D = Decimal("0.01")
        _HUNDRED_D = Decimal("100")

        # Cycle profit: realized gain above initial balance (floored to 0)
        _funded_bal  = Decimal(str(account.balance))
        _funded_init = Decimal(str(account.initial_balance or account.balance))
        _cycle_profit = max(_ZERO_D, _funded_bal - _funded_init)
        funded_cycle_profit = _cycle_profit

        # Split
        _split_pct = Decimal(str(_fc.profit_split_pct))
        funded_trader_cut = (_cycle_profit * _split_pct / _HUNDRED_D).quantize(_PENNY_D)
        funded_broker_cut = (_cycle_profit - funded_trader_cut).quantize(_PENNY_D)

        # Trading days in current cycle (since funded_at, fallback: all time)
        _funded_at_dt = (
            challenge_enrollment.funded_at if challenge_enrollment else None
        )
        _trade_qs = Trade.objects.filter(account=account, closed_at__isnull=False)
        if _funded_at_dt:
            _trade_qs = _trade_qs.filter(closed_at__gte=_funded_at_dt)
        funded_trading_days     = _trade_qs.dates("closed_at", "day").count()
        funded_min_trading_days = _fc.min_trading_days

        # Payout eligibility
        _profit_ok  = _cycle_profit >= Decimal(str(_fc.min_payout_usd))
        _days_ok    = funded_trading_days >= _fc.min_trading_days
        _account_ok = account.status == TradingAccount.STATUS_ACTIVE
        funded_payout_eligible = _profit_ok and _days_ok and _account_ok

        if not _account_ok:
            funded_payout_label = "Account suspended — payout blocked"
        elif funded_payout_eligible:
            funded_payout_label = "Eligible for payout"
        elif not _profit_ok:
            funded_payout_label = f"Minimum ${_fc.min_payout_usd} profit required"
        else:
            funded_payout_label = f"Minimum {_fc.min_trading_days} trading days required"

        # Next payout date
        _today_date = timezone.now().date()
        if _funded_at_dt:
            _funded_date  = _funded_at_dt.date()
            _cycle_days   = _fc.payout_cycle_days
            _days_since   = (_today_date - _funded_date).days
            _cycles_ahead = max(1, (_days_since // _cycle_days) + 1)
            _next_date    = _funded_date + timezone.timedelta(days=_cycles_ahead * _cycle_days)
            funded_next_payout_date  = _next_date
            funded_days_until_payout = max(0, (_next_date - _today_date).days)
    # ─────────────────────────────────────────────────────────────────────

    # ── Panel mode: which right-column card to show ───────────────────────────
    _acct_type = account.account_type
    show_challenge_panel    = _acct_type in ("CHALLENGE", "FUNDED")
    show_account_rules_panel = _acct_type in MARGIN_ENGINE_TYPES

    # Account rules snapshot — used by the Account Rules card for margin accounts
    acct_rules = {
        "product_name":       account.product_name_snapshot or _acct_type,
        "account_type":       _acct_type,
        "currency":           account.currency or "USD",
        "leverage":           account.leverage_snapshot or account.leverage,
        "spread_pips":        account.spread_pips_snapshot,
        "commission_per_lot": account.commission_per_lot_snapshot,
        "margin_call_level":  account.margin_call_level_snapshot,
        "stopout_level":      account.stopout_level_snapshot,
        "max_lot_size":       account.max_lot_size_snapshot,
    }

    context = {
        'account': account,
        'account_id': account.id,
        'trades': trades,
        'open_trades_json': json.dumps(formatted_trades, cls=DjangoJSONEncoder),
        'active_section': 'trading',
        # Panel mode flags
        'show_challenge_panel':    show_challenge_panel,
        'show_account_rules_panel': show_account_rules_panel,
        'acct_rules':              acct_rules,
        # Phase 4A intelligence
        'realized_dd_pct':       realized_dd_pct,
        'daily_realized_dd_pct': daily_realized_dd_pct,
        'today_realized_pnl':    today_realized_pnl,
        'profit_pct':            profit_pct,
        'has_profit_target':     has_profit_target,
        'intel_max_dd_pct':      intel_max_dd_pct,
        'intel_max_daily_pct':   intel_max_daily_pct,
        'equity_curve':          equity_curve,
        'equity_curve_json':     equity_curve_json,
        'trader_score':          trader_score,
        'recent_violations':     recent_violations,
        'win_rate_pct':          win_rate_pct,
        'total_realized_pnl':    total_realized_pnl,
        # Bar helpers (Paso 2)
        'daily_dd_bar_pct':      daily_dd_bar_pct,
        'max_dd_bar_pct':        max_dd_bar_pct,
        'daily_dd_safe':         daily_dd_safe,
        'max_dd_safe':           max_dd_safe,
        # Phase 4C.1 — Challenge lifecycle
        'challenge_enrollment':        challenge_enrollment,
        'challenge_phase_label':       challenge_phase_label,
        'challenge_eval_status':       challenge_eval_status,
        'challenge_eval_fail_reason':  challenge_eval_fail_reason,
        'challenge_trading_days':      challenge_trading_days,
        'challenge_min_trading_days':  challenge_min_trading_days,
        'challenge_days_remaining':    challenge_days_remaining,
        'challenge_max_duration_days': challenge_max_duration_days,
        'challenge_funded_config':     challenge_funded_config,
        # Phase 4C.2 — Funded section
        'funded_section':            funded_section,
        'funded_cycle_profit':       funded_cycle_profit,
        'funded_trader_cut':         funded_trader_cut,
        'funded_broker_cut':         funded_broker_cut,
        'funded_payout_eligible':    funded_payout_eligible,
        'funded_payout_label':       funded_payout_label,
        'funded_trading_days':       funded_trading_days,
        'funded_min_trading_days':   funded_min_trading_days,
        'funded_next_payout_date':   funded_next_payout_date,
        'funded_days_until_payout':  funded_days_until_payout,
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
    Returns None only when the user has no active accounts at all.
    """
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user, status='Activo').first()
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
        return redirect("simulator:accounts")

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

    now = timezone.now()

    # Broker ecosystem widgets
    upcoming_events   = CalendarEvent.objects.filter(published=True, event_date__gte=now).order_by('event_date')[:3]
    active_bonuses_ct = Bonus.objects.filter(active=True).count()
    referral, _       = Referral.objects.get_or_create(
        user=request.user,
        defaults={'code': _secrets.token_urlsafe(8)},
    )
    recent_docs       = BrokerDocument.objects.filter(public=True).order_by('-created_at')[:3]
    ea_count          = ExpertAdvisor.objects.filter(active=True).count()

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
        # Ecosystem widgets
        'upcoming_events': upcoming_events,
        'active_bonuses_ct': active_bonuses_ct,
        'referral': referral,
        'recent_docs': recent_docs,
        'ea_count': ea_count,
        'active_section': 'dashboard',
    })


# HISTORIAL DE TRADES
# -----------------------
@login_required
def history_view(request):
    acc_id = request.session.get("account_id")
    account = TradingAccount.objects.filter(pk=acc_id, user=request.user).first()
    if not account:
        return redirect("simulator:accounts")

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
    """My Accounts page — wallet summary + existing trading accounts only."""
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
        "wallet":          wallet,
        "accounts_data":   accounts_data,
        "active_section":  "accounts",
        "acct_success":    acct_success,
        "acct_error":      acct_error,
        "email_verified":  _is_email_verified(request.user),
    })


@login_required
def account_open_view(request):
    """Product catalog page — /accounts/open/ — choose Demo or Real account to open."""
    wallet, _ = get_or_create_wallet(request.user)
    catalog = (
        AccountProduct.objects
        .filter(is_active=True)
        .order_by("family", "sort_order", "name")
    )
    demo_products = [p for p in catalog if p.family == AccountProduct.FAMILY_DEMO]
    real_products = [p for p in catalog if p.family == AccountProduct.FAMILY_REAL]

    return render(request, "simulator/account_open.html", {
        "wallet":         wallet,
        "demo_products":  demo_products,
        "real_products":  real_products,
        "active_section": "account_open",
    })


@login_required
def create_account_view(request):
    """
    POST /accounts/create/

    Creates a TradingAccount from an AccountProduct catalog entry.
    Expects: product_id=<int>  [amount=<decimal> — required for REAL accounts]

    Demo accounts: no wallet debit, uses product.default_balance.
    Real accounts:  validates amount >= product.min_deposit, transfers wallet → account.
    CHALLENGE/FUNDED cannot be created via this endpoint.
    """
    if request.method != "POST":
        return redirect("simulator:accounts")

    # ── Resolve product ────────────────────────────────────────────────────────
    try:
        product_id = int(request.POST.get("product_id", ""))
    except (ValueError, TypeError):
        request.session["acct_error"] = "Producto inválido."
        return redirect("simulator:accounts")

    product = AccountProduct.objects.filter(pk=product_id, is_active=True).first()
    if not product:
        request.session["acct_error"] = "Producto no encontrado o inactivo."
        return redirect("simulator:accounts")

    # Guard: CHALLENGE and FUNDED cannot be created via this flow
    if product.product_type in ("CHALLENGE", "FUNDED"):
        request.session["acct_error"] = (
            "Las cuentas Challenge y Funded solo se crean mediante el proceso de compra."
        )
        return redirect("simulator:accounts")

    is_demo = product.family == AccountProduct.FAMILY_DEMO
    wallet, _ = get_or_create_wallet(request.user)

    # ── Compliance gates — real accounts only ─────────────────────────────────
    if not is_demo:
        if not _is_email_verified(request.user):
            request.session["acct_error"] = _EMAIL_GATE_MSG
            return redirect("simulator:accounts")
        if not _has_accepted_terms(request.user):
            request.session["acct_error"] = _TERMS_GATE_MSG
            return redirect("simulator:accounts")

    # ── Validate amount for REAL ───────────────────────────────────────────────
    if not is_demo:
        raw_amount = request.POST.get("amount", "").strip()
        if not raw_amount:
            request.session["acct_error"] = "El monto es requerido para cuentas reales."
            return redirect("simulator:accounts")

        try:
            amount = Decimal(str(raw_amount))
        except Exception:
            request.session["acct_error"] = "Monto inválido."
            return redirect("simulator:accounts")

        if amount <= 0:
            request.session["acct_error"] = "El monto debe ser mayor a cero."
            return redirect("simulator:accounts")

        if amount < product.min_deposit:
            request.session["acct_error"] = (
                f"El monto mínimo para {product.name} es ${product.min_deposit:.2f}. "
                f"Ingresaste ${amount:.2f}."
            )
            return redirect("simulator:accounts")

        if wallet.available_balance < amount:
            request.session["acct_error"] = (
                f"Saldo insuficiente en wallet. Disponible: ${wallet.available_balance:.2f}, "
                f"solicitado: ${amount:.2f}. Por favor deposita primero."
            )
            return redirect("simulator:accounts")

    # ── Create account ─────────────────────────────────────────────────────────
    try:
        from .tasks import send_email_async as _send_email
        with transaction.atomic():
            if is_demo:
                account = TradingAccount.objects.create(
                    user=request.user,
                    wallet=wallet,
                    account_type=AccountProduct.TYPE_DEMO,
                    leverage=product.max_leverage,
                    initial_balance=product.default_balance,
                )
            else:
                account = TradingAccount.objects.create(
                    user=request.user,
                    wallet=wallet,
                    account_type=product.product_type,
                    leverage=product.max_leverage,
                    initial_balance=Decimal("0"),
                )
                transfer_to_account(
                    wallet.id, account.id, amount,
                    note=f"Initial funding — {product.name} #{account.id}",
                    initiated_by=request.user,
                )
                account.refresh_from_db()

            # Freeze product rules as immutable snapshots on the account.
            # Uses .update() to bypass TradingAccount.save() balance logic.
            TradingAccount.objects.filter(pk=account.pk).update(
                account_product=product,
                product_code_snapshot=product.code,
                product_name_snapshot=product.name,
                leverage_snapshot=product.max_leverage,
                spread_pips_snapshot=product.typical_spread_pips,
                commission_per_lot_snapshot=product.commission_per_lot,
                allowed_symbols_snapshot=product.allowed_symbols,
                max_lot_size_snapshot=product.max_lot_size,
                margin_call_level_snapshot=product.margin_call_level,
                stopout_level_snapshot=product.stopout_level,
            )
            account.refresh_from_db()

    except InsufficientFunds as exc:
        request.session["acct_error"] = str(exc)
        return redirect("simulator:accounts")
    except Exception as exc:
        logger.error("create_account_view error: %s", exc, exc_info=True)
        request.session["acct_error"] = "La creación de cuenta falló. Intenta de nuevo."
        return redirect("simulator:accounts")

    # ── Confirmation email ─────────────────────────────────────────────────────
    email = request.user.email
    if email:
        if is_demo:
            subject = "Tu cuenta Demo fue creada — Money Broker"
        else:
            subject = "Tu cuenta Real fue creada — Money Broker"

        message = (
            f"Hola {request.user.username},\n\n"
            f"Tu cuenta ha sido creada exitosamente.\n\n"
            f"  Producto       : {product.name}\n"
            f"  Plataforma     : {product.platform_label}\n"
            f"  Account ID     : #{account.id}\n"
            f"  Tipo interno   : {account.account_type}\n"
            f"  Balance inicial: ${account.balance:.2f}\n"
            f"  Apalancamiento : 1:{account.leverage}\n"
            f"  Spread típico  : {product.typical_spread_pips} pips\n"
            f"  Comisión/lote  : ${product.commission_per_lot:.2f}\n\n"
            f"  Acceder al dashboard: /dashboard/{account.id}/\n"
            f"  Login: /login/\n\n"
            f"— Money Broker"
        )
        _send_email.delay(
            subject=subject,
            message=message,
            recipient_list=[email],
        )

    request.session["acct_success"] = (
        f"Cuenta {product.name} #{account.id} creada exitosamente."
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

    if not _is_email_verified(request.user):
        request.session["acct_error"] = _EMAIL_GATE_MSG
        return redirect("simulator:accounts")
    if not _has_accepted_terms(request.user):
        request.session["acct_error"] = _TERMS_GATE_MSG
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
        log_audit(
            request, EV_ACCOUNT_FUNDED,
            f"Account #{account.id} funded ${amount:.2f} from wallet",
            account=account,
            detail={"amount": str(amount), "wallet_id": wallet.id, "account_id": account.id},
        )
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
        log_audit(
            request, EV_ACCOUNT_WITHDRAWN,
            f"Account #{account.id} withdrew ${amount:.2f} to wallet",
            account=account,
            detail={"amount": str(amount), "wallet_id": wallet.id, "account_id": account.id},
        )
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
# EXTERNAL CHALLENGE ACTIVATION WEBHOOK
# ===================================================

import hashlib as _hashlib
import hmac as _hmac_mod


def _verify_challenge_webhook_sig(body: bytes, signature: str) -> bool:
    """
    Verify HMAC-SHA256 signature from an external sales platform.

    The external platform signs: JSON body with keys sorted alphabetically,
    compact encoding — same convention as NowPayments IPN.

    Rejects immediately if CHALLENGE_WEBHOOK_SECRET is not configured.
    """
    secret = getattr(settings, "CHALLENGE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error("[ext_challenge] CHALLENGE_WEBHOOK_SECRET not configured — rejecting")
        return False
    if not signature:
        return False
    try:
        payload   = json.loads(body)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        computed  = _hmac_mod.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            _hashlib.sha256,
        ).hexdigest()
        return _hmac_mod.compare_digest(computed, signature.lower())
    except Exception:
        return False


def _find_or_create_external_user(email: str, full_name: str):
    """
    Return (user, created, temp_password).

    Finds existing user by email (first match). If none found, creates one
    with an auto-derived username and a random password. temp_password is
    non-empty only when the user is newly created.
    """
    from django.contrib.auth import get_user_model as _gum
    User = _gum()

    existing = User.objects.filter(email__iexact=email).first()
    if existing:
        return existing, False, ""

    # Derive a unique username from the email prefix
    base = email.split("@")[0][:28].replace("+", "_").replace(".", "_")
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{suffix}"
        suffix += 1

    parts = full_name.strip().split(None, 1) if full_name.strip() else ["", ""]
    first = parts[0]
    last  = parts[1] if len(parts) > 1 else ""

    import secrets as _sec
    temp_password = _sec.token_urlsafe(10)
    user = User.objects.create_user(
        username=username,
        email=email,
        password=temp_password,
        first_name=first,
        last_name=last,
    )
    return user, True, temp_password


@csrf_exempt
def external_challenge_activate(request):
    """
    POST /api/internal/challenge/activate/

    Called by an external sales platform after a challenge purchase is confirmed.
    Creates the user (if new), ChallengeEnrollment, and Phase 1 account.

    Security: HMAC-SHA256 via X-MoneyBroker-Signature header.
    Idempotency: checked via external_event_id (strict) and external_payment_id (secondary).
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    body = request.body
    sig  = request.headers.get("X-MoneyBroker-Signature", "")

    if not _verify_challenge_webhook_sig(body, sig):
        logger.warning("[ext_challenge] rejected — invalid signature sig=%s…", sig[:16])
        return JsonResponse({"error": "invalid signature"}, status=401)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    # ── Validate required fields ──────────────────────────────────────────────
    event_id     = (data.get("event_id") or "").strip()
    email        = (data.get("email") or "").strip().lower()
    full_name    = (data.get("full_name") or "").strip()
    product_code = (data.get("challenge_product_code") or "").strip()
    payment_id   = (data.get("payment_id") or "").strip()
    amount_raw   = data.get("amount_paid")

    missing = [f for f, v in [("event_id", event_id), ("email", email),
                               ("challenge_product_code", product_code)] if not v]
    if missing:
        return JsonResponse({"error": f"missing required fields: {missing}"}, status=400)

    # ── Idempotency: event_id (strict) ────────────────────────────────────────
    existing = ChallengeEnrollment.objects.filter(external_event_id=event_id).first()
    if existing:
        logger.info("[ext_challenge] duplicate event_id=%s enrollment=%d", event_id, existing.pk)
        return JsonResponse({
            "ok": True, "idempotent": True,
            "enrollment_id": existing.pk,
            "account_id": existing.phase1_account_id,
        })

    # ── Idempotency: payment_id (secondary) ───────────────────────────────────
    if payment_id:
        existing = ChallengeEnrollment.objects.filter(external_payment_id=payment_id).first()
        if existing:
            logger.info("[ext_challenge] duplicate payment_id=%s enrollment=%d", payment_id, existing.pk)
            return JsonResponse({
                "ok": True, "idempotent": True,
                "enrollment_id": existing.pk,
                "account_id": existing.phase1_account_id,
            })

    # ── Resolve product ───────────────────────────────────────────────────────
    product = ChallengeProduct.objects.filter(external_code=product_code, is_active=True).first()
    if not product:
        logger.warning("[ext_challenge] product not found code=%r", product_code)
        return JsonResponse({"error": f"product '{product_code}' not found or inactive"}, status=400)

    # ── Validate amount ───────────────────────────────────────────────────────
    if amount_raw is not None:
        try:
            amount_paid = Decimal(str(amount_raw))
        except Exception:
            return JsonResponse({"error": "invalid amount_paid"}, status=400)
        if amount_paid < product.price_usd:
            return JsonResponse({
                "error": f"amount_paid {amount_paid} is less than product price {product.price_usd}"
            }, status=400)

    # ── Find or create user ───────────────────────────────────────────────────
    user, user_created, temp_password = _find_or_create_external_user(email, full_name)

    # ── Atomic: create enrollment + activate ──────────────────────────────────
    from .tasks import send_email_async as _send_email

    with transaction.atomic():
        enrollment = ChallengeEnrollment.objects.create(
            user=user,
            product=product,
            deposit=None,
            status=ChallengeEnrollment.ST_PHASE_1,
            external_event_id=event_id or None,
            external_payment_id=payment_id or None,
        )
        _ce_activate(enrollment)

    logger.info(
        "[ext_challenge] activated enrollment=%d product=%d user=%d user_created=%s",
        enrollment.pk, product.pk, user.pk, user_created,
    )

    # ── Email ─────────────────────────────────────────────────────────────────
    login_url = request.build_absolute_uri(reverse("simulator:login"))
    enrollment.refresh_from_db()
    account_id = enrollment.phase1_account_id

    if user_created:
        subject = f"Tu Challenge {product.name} está listo — acceso a Money Broker"
        message = (
            f"Hola {user.first_name or user.username},\n\n"
            f"Tu Challenge {product.name} ({product.tier}) ha sido activado.\n\n"
            f"Credenciales de acceso a Money Broker:\n"
            f"  Usuario:    {user.username}\n"
            f"  Contraseña: {temp_password}\n\n"
            f"Accede aquí: {login_url}\n\n"
            f"Por seguridad, cambia tu contraseña después de tu primer inicio de sesión.\n\n"
            f"— TRX Simulator"
        )
    else:
        subject = f"Nuevo Challenge {product.name} activado en Money Broker"
        message = (
            f"Hola {user.first_name or user.username},\n\n"
            f"Tu Challenge {product.name} ({product.tier}) ha sido activado "
            f"y añadido a tu cuenta.\n\n"
            f"Accede aquí: {login_url}\n\n"
            f"— TRX Simulator"
        )

    if user.email:
        _send_email.delay(
            subject=subject,
            message=message,
            recipient_list=[user.email],
        )

    return JsonResponse({
        "ok": True,
        "idempotent": False,
        "enrollment_id": enrollment.pk,
        "account_id": account_id,
        "user_created": user_created,
        "login_url": login_url,
    })


# ===================================================
# CHALLENGE PURCHASE
# ===================================================

def _fulfill_challenge_purchase(deposit):
    """
    Called inside the deposit_callback atomic block when a challenge payment is confirmed.

    Creates ChallengeEnrollment and activates Phase 1.
    Idempotency: guarded by UniqueConstraint(deposit) on ChallengeEnrollment — a second
    call for the same deposit raises IntegrityError which the caller catches and logs.

    Must be called while still inside transaction.atomic() so that any exception here
    rolls back the entire callback transaction, leaving deposit.credited=False for retry.
    """
    from .tasks import send_email_async
    product = deposit.challenge_product
    enrollment = ChallengeEnrollment.objects.create(
        user=deposit.user,
        product=product,
        deposit=deposit,
        status=ChallengeEnrollment.ST_PHASE_1,
    )
    _ce_activate(enrollment)
    logger.info(
        "[challenge_purchase] enrollment=%d product=%d user=%s phase1 activated",
        enrollment.pk, product.pk, deposit.user_id,
    )
    email = deposit.user.email
    if email:
        send_email_async.delay(
            subject=f"Tu Challenge {product.name} está activo",
            message=(
                f"Hola {deposit.user.username},\n\n"
                f"Tu pago fue confirmado y tu Challenge {product.name} ({product.tier}) "
                f"ha sido activado.\n\n"
                f"Accede al simulador para ver tu cuenta Fase 1.\n\n"
                f"Buena suerte.\n— TRX Simulator"
            ),
            recipient_list=[email],
        )


@login_required
def challenge_catalog_view(request):
    """List active ChallengeProducts the user can purchase."""
    products = ChallengeProduct.objects.filter(is_active=True).order_by("tier", "price_usd")
    return render(request, "simulator/challenge_catalog.html", {
        "products": products,
        "active_section": "challenges",
    })


@login_required
def challenge_purchase_view(request, product_id):
    """
    GET  — show product detail + crypto selector.
    POST — create a NowPayments Deposit tagged with challenge_product, redirect to status page.
    """
    product = ChallengeProduct.objects.filter(pk=product_id, is_active=True).first()
    if not product:
        return redirect("simulator:challenge_catalog")

    error = None
    if request.method == "POST":
        # ── Compliance gates (email → terms) ─────────────────────────────────
        if not _is_email_verified(request.user):
            error = _EMAIL_GATE_MSG
        elif not _has_accepted_terms(request.user):
            error = _TERMS_GATE_MSG
        else:
            crypto_currency = request.POST.get("crypto_currency", "").strip()
            try:
                _np.to_np_code(crypto_currency)   # validates the currency exists
            except (ValueError, AttributeError):
                error = f"Moneda no soportada: {crypto_currency}"
            else:
                deposit = Deposit.objects.create(
                    user=request.user,
                    amount_usd=product.price_usd,
                    crypto_currency=crypto_currency,
                    status=Deposit.STATUS_PENDING,
                    challenge_product=product,
                )
                logger.info(
                    "[challenge_purchase] deposit=%d product=%d user=%s amount=%.2f",
                    deposit.pk, product.pk, request.user.username, float(product.price_usd),
                )
                try:
                    callback_url = request.build_absolute_uri(
                        reverse("simulator:deposit_callback")
                    )
                    data = _np.create_payment(
                        product.price_usd, crypto_currency, deposit.id, callback_url
                    )
                    from django.utils.dateparse import parse_datetime
                    exp_raw = data.get("expiration_estimate_date")
                    Deposit.objects.filter(pk=deposit.pk).update(
                        nowpayments_payment_id  = str(data.get("payment_id", "")),
                        nowpayments_invoice_url = data.get("invoice_url", "") or "",
                        pay_address             = data.get("pay_address", "") or "",
                        pay_amount              = data.get("pay_amount"),
                        expires_at              = parse_datetime(exp_raw) if exp_raw else None,
                        status                  = data.get("payment_status", Deposit.STATUS_WAITING),
                    )
                    return redirect("simulator:deposit_status", deposit_id=deposit.pk)
                except Exception as exc:
                    logger.error(
                        "[challenge_purchase] NP failed deposit=%d %s: %s",
                        deposit.pk, type(exc).__name__, exc, exc_info=True,
                    )
                    Deposit.objects.filter(pk=deposit.pk).update(status=Deposit.STATUS_FAILED)
                    error = "No se pudo crear el pago. Por favor intenta más tarde."

    from .currencies import CRYPTO_CHOICES
    return render(request, "simulator/challenge_purchase.html", {
        "product":         product,
        "crypto_choices":  CRYPTO_CHOICES,
        "error":           error,
        "active_section":  "challenges",
    })


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
        # ── Compliance gates (email → terms) ─────────────────────────────────
        _gate_err = None
        if not _is_email_verified(request.user):
            _gate_err = _EMAIL_GATE_MSG
        elif not _has_accepted_terms(request.user):
            _gate_err = _TERMS_GATE_MSG
        if _gate_err:
            return render(request, "simulator/deposit.html", {
                "form": DepositForm(), "wallet": wallet,
                "error": _gate_err, "active_section": "deposit",
            })

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
@rate_limit("deposit_callback", limit=30, window=60)
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
        _confirmed_dec = Decimal(str(actually_paid)) if actually_paid else None
        if _confirmed_dec:
            update_fields["confirmed_amount_usd"] = _confirmed_dec

        # Credit the amount NowPayments confirmed. Fall back to the requested amount
        # only when the provider sends no confirmation field at all.
        credit_amount = _confirmed_dec if (_confirmed_dec and _confirmed_dec > 0) else deposit.amount_usd

        if payment_status in Deposit.CREDITED_STATUSES:
            if deposit.challenge_product_id:
                # ── Challenge purchase ────────────────────────────────────────────
                # Confirmed amount must cover the full product price before activating.
                required = deposit.challenge_product.price_usd
                if credit_amount < required:
                    logger.warning(
                        "[callback] CHALLENGE UNDERPAID deposit_id=%d required=%s "
                        "confirmed=%s — enrollment NOT created",
                        deposit.id, required, credit_amount,
                    )
                    # credited stays False; status row is updated for support visibility
                else:
                    _fulfill_challenge_purchase(deposit)
                    logger.info(
                        "[callback] CHALLENGE ACTIVATED deposit_id=%d product_id=%d user=%s",
                        deposit.id, deposit.challenge_product_id, deposit.user_id,
                    )
                    update_fields["credited"]     = True
                    update_fields["credited_at"]  = timezone.now()
                    update_fields["confirmed_at"] = timezone.now()
            else:
                # ── Regular wallet top-up: credit confirmed amount ────────────────
                wallet, _ = get_or_create_wallet(deposit.user)
                credit_wallet(
                    wallet.id,
                    credit_amount,
                    WalletTransaction.TX_DEPOSIT,
                    deposit=deposit,
                    note=f"NowPayments #{payment_id} {deposit.crypto_currency.upper()}",
                )

                # Drain pending_balance if it was incremented during confirming.
                # Always drain deposit.amount_usd (what was added), not credit_amount.
                if deposit.status == Deposit.STATUS_CONFIRMING:
                    from django.db.models import F
                    from .models import Wallet as WalletModel
                    WalletModel.objects.filter(pk=wallet.pk).update(
                        pending_balance=F("pending_balance") - deposit.amount_usd
                    )

                logger.info(
                    "[callback] WALLET CREDITED deposit_id=%d payment_id=%s "
                    "confirmed_usd=%s requested_usd=%s currency=%s wallet_id=%d",
                    deposit.id, payment_id,
                    credit_amount, deposit.amount_usd,
                    deposit.crypto_currency.upper(), wallet.id,
                )
                log_audit(
                    request, EV_DEPOSIT_CREDITED,
                    f"Deposit #{deposit.id} credited ${credit_amount}",
                    detail={
                        "deposit_id":     deposit.id,
                        "payment_id":     payment_id,
                        "amount_usd":     str(deposit.amount_usd),
                        "confirmed_usd":  str(credit_amount),
                        "currency":       deposit.crypto_currency,
                        "wallet_id":      wallet.id,
                        "payment_status": payment_status,
                    },
                )

                update_fields["credited"]     = True
                update_fields["credited_at"]  = timezone.now()
                update_fields["confirmed_at"] = timezone.now()

        elif payment_status == Deposit.STATUS_PARTIALLY_PAID:
            # Partial payment: save confirmed amount for support, do NOT credit.
            logger.warning(
                "[callback] PARTIAL PAYMENT deposit_id=%d requested=%s confirmed=%s "
                "— NOT credited, pending support review",
                deposit.id, deposit.amount_usd, credit_amount,
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

    # Send confirmation email outside the atomic block so a queuing failure
    # never rolls back the credit.  update_fields["credited"] is True only when
    # this callback is the one that just credited the deposit for the first time;
    # duplicate IPNs return before reaching here via the idempotency gate above.
    if update_fields.get("credited"):
        try:
            from .deposit_emails import send_deposit_confirmed_email
            send_deposit_confirmed_email(deposit)
        except Exception as mail_exc:
            logger.warning(
                "[callback] deposit email failed deposit_id=%d: %s",
                deposit.id, mail_exc,
            )

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

class _PendingWithdrawalExists(Exception):
    """Raised inside the atomic block when a PENDING withdrawal already exists."""


@login_required
@rate_limit("withdraw", limit=5, window=60, by="user")
def withdraw_view(request):
    """
    GET  /withdraw/  — show withdrawal form with wallet balance.
    POST /withdraw/  — validate, debit wallet atomically, create WithdrawalRequest.
    Funds are reserved immediately; admin approval triggers the NP payout.

    Double-withdrawal prevention:
      Inside the atomic block, the wallet row is locked with select_for_update()
      BEFORE checking pending withdrawals. This serializes all concurrent
      withdrawal attempts for the same user — concurrent requests block on the
      wallet lock, so the pending check is race-condition safe.
    """
    wallet, _ = get_or_create_wallet(request.user)
    error = None

    try:
        kyc_approved = request.user.kyc_profile.status == KYCProfile.STATUS_APPROVED
    except KYCProfile.DoesNotExist:
        kyc_approved = False

    from django.conf import settings as _settings
    daily_limit   = Decimal(str(_settings.MAX_WITHDRAWAL_DAILY_USD))
    min_withdrawal = Decimal(str(_settings.MIN_WITHDRAWAL_USD))
    daily_used  = _get_daily_withdrawal_used(request.user)
    daily_avail = max(daily_limit - daily_used, Decimal("0"))

    if request.method == "POST":
        form = WithdrawForm(request.POST)
        if form.is_valid():
            # ── Compliance gates (email → terms → 2FA) ────────────────────────
            if not _is_email_verified(request.user):
                error = _EMAIL_GATE_MSG
            elif not _has_accepted_terms(request.user):
                error = _TERMS_GATE_MSG

            # ── 2FA gate — checked before any financial operation ──────────────
            if not error:
                from .models import TOTPDevice
                from .two_factor import verify_totp as _verify_totp
                _has_device = TOTPDevice.objects.filter(
                    user=request.user, confirmed=True
                ).exists()
                if not _has_device:
                    error = "Debes activar 2FA (autenticación de dos factores) antes de solicitar retiros."
                else:
                    _otp_code = request.POST.get("otp_code", "").strip()
                    if not _verify_totp(request.user, _otp_code):
                        security_log("withdrawal.2fa_failed",
                                     username=request.user.username, user_id=request.user.pk)
                        error = "Código 2FA incorrecto. Verifica tu app y vuelve a intentarlo."

            # ── KYC gate — identity must be approved before any financial operation ──
            if not error and not kyc_approved:
                error = _KYC_GATE_MSG

            if error:
                pass  # fall through to re-render form with error
            else:
                amount_usd      = form.cleaned_data["amount_usd"]
                crypto_currency = form.cleaned_data["crypto_currency"]
                wallet_address  = form.cleaned_data["wallet_address"]

            # ── Minimum withdrawal amount ──────────────────────────────────────
            if not error:
                from django.conf import settings as _settings
                _min_wd = Decimal(str(_settings.MIN_WITHDRAWAL_USD))
                if amount_usd < _min_wd:
                    error = f"El monto mínimo de retiro es ${_min_wd:,.2f} USD."

            # ── Daily withdrawal cap ───────────────────────────────────────────
            if not error:
                from django.conf import settings as _settings
                _daily_limit = Decimal(str(_settings.MAX_WITHDRAWAL_DAILY_USD))
                _daily_used  = _get_daily_withdrawal_used(request.user)
                _daily_avail = _daily_limit - _daily_used
                if _daily_avail < amount_usd:
                    error = (
                        f"Límite diario de retiros alcanzado. "
                        f"Límite: ${_daily_limit:,.2f} · "
                        f"Usado hoy: ${_daily_used:,.2f} · "
                        f"Disponible: ${max(_daily_avail, Decimal('0')):,.2f}"
                    )

            # ── Original financial flow — only reached when 2FA passed ─────────
            if not error and wallet.available_balance < amount_usd:
                error = f"Balance insuficiente. Disponible: ${wallet.available_balance:,.2f}"

            if not error:
                try:
                    with transaction.atomic():
                        # Lock wallet row — serializes ALL concurrent withdrawal
                        # attempts for this user on both the pending check and debit.
                        Wallet.objects.select_for_update().get(pk=wallet.id)

                        # Guard: one PENDING withdrawal per user at a time.
                        if WithdrawalRequest.objects.filter(
                            user=request.user,
                            status=WithdrawalRequest.STATUS_PENDING,
                        ).exists():
                            raise _PendingWithdrawalExists()

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
                    log_audit(
                        request, EV_WITHDRAW_REQUEST,
                        f"Withdrawal #{wr.id} requested — ${amount_usd} {crypto_currency.upper()}",
                        detail={
                            "withdrawal_id":  wr.id,
                            "amount_usd":     str(amount_usd),
                            "currency":       crypto_currency,
                            "wallet_address": wallet_address,
                            "debit_tx_id":    debit_tx.id,
                        },
                    )

                    _masked_addr = _mask_wallet(wallet_address)
                    try:
                        from .withdrawal_emails import send_withdrawal_status_email, EVENT_REQUESTED
                        send_withdrawal_status_email(wr, EVENT_REQUESTED)
                    except Exception as mail_exc:
                        logger.warning("[withdraw] user confirmation email failed wr=%d: %s", wr.id, mail_exc)

                    try:
                        from .tasks import send_email_async as _send_email
                        _admin_email = settings.ADMINS[0][1] if settings.ADMINS else None
                        if _admin_email:
                            _send_email.delay(
                                subject=f"[Admin] Nueva solicitud de retiro #{wr.id} — ${amount_usd}",
                                message=(
                                    f"Usuario:    {request.user.username} ({request.user.email})\n"
                                    f"Monto:      ${amount_usd} USD\n"
                                    f"Moneda:     {crypto_currency.upper()}\n"
                                    f"Dirección:  {_masked_addr}\n"
                                    f"WR ID:      #{wr.id}\n\n"
                                    f"Revisar en el panel de administración."
                                ),
                                recipient_list=[_admin_email],
                            )
                    except Exception as mail_exc:
                        logger.warning("[withdraw] admin notification email failed wr=%d: %s", wr.id, mail_exc)

                    return redirect("simulator:withdraw_history")

                except _PendingWithdrawalExists:
                    error = (
                        "Ya tienes un retiro pendiente. "
                        "Espera a que sea procesado antes de solicitar otro."
                    )
                except InsufficientFunds:
                    error = "Balance insuficiente."
                except Exception as exc:
                    logger.error("[withdraw] failed user=%s: %s", request.user.username, exc, exc_info=True)
                    error = "Error al procesar la solicitud. Intenta de nuevo."
    else:
        form = WithdrawForm()

    wallet.refresh_from_db()
    daily_used  = _get_daily_withdrawal_used(request.user)
    daily_avail = max(daily_limit - daily_used, Decimal("0"))
    from .models import TOTPDevice as _TD
    _totp_enabled = _TD.objects.filter(user=request.user, confirmed=True).exists()
    return render(request, "simulator/withdraw.html", {
        "form":           form,
        "wallet":         wallet,
        "error":          error,
        "kyc_approved":   kyc_approved,
        "daily_limit":     daily_limit,
        "daily_used":      daily_used,
        "daily_avail":     daily_avail,
        "min_withdrawal":  min_withdrawal,
        "totp_enabled":    _totp_enabled,
        "active_section":  "withdraw",
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
@rate_limit("withdraw_callback", limit=30, window=60)
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
            # All three terminal statuses are irreversible — skip idempotently.
            # FAILED was previously missing, allowing duplicate FAILED IPNs to double-refund.
            _TERMINAL = (
                WithdrawalRequest.STATUS_COMPLETED,
                WithdrawalRequest.STATUS_REJECTED,
                WithdrawalRequest.STATUS_FAILED,
            )
            if wr.status in _TERMINAL:
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
                log_audit(
                    request, EV_WITHDRAW_FAILED,
                    f"Withdrawal #{wr.id} FAILED by NowPayments — payout {payout_id}",
                    detail={
                        "withdrawal_id": wr.id,
                        "payout_id": payout_id,
                        "np_status": np_status,
                        "amount_usd": str(wr.amount_usd),
                    },
                )
                log_audit(
                    request, EV_WITHDRAW_REFUNDED,
                    f"Withdrawal #{wr.id} refunded ${wr.amount_usd} after payout failure",
                    detail={
                        "withdrawal_id": wr.id,
                        "payout_id": payout_id,
                        "amount_usd": str(wr.amount_usd),
                        "wallet_id": wallet.id,
                    },
                )
                try:
                    from .withdrawal_emails import send_withdrawal_status_email, EVENT_FAILED
                    send_withdrawal_status_email(wr, EVENT_FAILED)
                except Exception as mail_exc:
                    logger.warning("[payout_cb] failed email queuing failed wr=%d: %s", wr.id, mail_exc)

            elif new_status == WithdrawalRequest.STATUS_COMPLETED:
                logger.info("[payout_cb] COMPLETED wr_id=%d payout_id=%s", wr.id, payout_id)
                log_audit(
                    request, EV_WITHDRAW_COMPLETE,
                    f"Withdrawal #{wr.id} COMPLETED — ${wr.amount_usd}",
                    detail={
                        "withdrawal_id": wr.id,
                        "payout_id": payout_id,
                        "amount_usd": str(wr.amount_usd),
                        "crypto_amount": str(wr.crypto_amount),
                        "currency": wr.crypto_currency,
                        "wallet_address": _mask_wallet(wr.wallet_address),
                    },
                )
                try:
                    from .withdrawal_emails import send_withdrawal_status_email, EVENT_COMPLETED
                    send_withdrawal_status_email(wr, EVENT_COMPLETED)
                except Exception as mail_exc:
                    logger.warning("[payout_cb] completed email queuing failed wr=%d: %s", wr.id, mail_exc)

            WithdrawalRequest.objects.filter(pk=wr.pk).update(**update)

    return JsonResponse({"ok": True})


# ──────────────────────────────────────────────────────────────
# Health check — public liveness probe
# GET /api/health/  →  200 {"status":"ok"}
# No internal details: DB/Redis state is an operational secret.
# ──────────────────────────────────────────────────────────────
def health_check(request):
    return JsonResponse({"status": "ok"})


# ──────────────────────────────────────────────────────────────
# Health detail — staff-only subsystem checks
# GET /api/health/detail/  →  200 {"status":"ok", "db":{...}, "redis":{...}}
#                          →  503 {"status":"degraded", ...}
#                          →  403 if not staff
# ──────────────────────────────────────────────────────────────
def health_detail_view(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return JsonResponse({"error": "forbidden"}, status=403)

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

    # ── Stress peaks ──────────────────────────────────────────────────────────
    try:
        from .observability import get_peaks
        redis_url = getattr(settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        result["peaks"] = get_peaks(redis_url)
    except Exception as exc:
        result["peaks"] = {"error": str(exc)}

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


# ──────────────────────────────────────────────────────────────
# Admin Operational Panel — staff-only HTML dashboard
#
# GET /staff/ops/
# Read-only aggregation of operational state.
# No polling, no WebSocket — manual refresh.
# ──────────────────────────────────────────────────────────────
def ops_panel_view(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return redirect("simulator:login")

    from .audit import log_audit, EV_ADMIN_VIEW
    log_audit(request, EV_ADMIN_VIEW, "Staff accessed ops panel")

    import subprocess
    import os as _os
    import json as _json

    ctx: dict = {}

    # ── Version / uptime ───────────────────────────────────────────────────────
    try:
        git_ver = subprocess.check_output(
            ["git", "describe", "--tags", "--always"],
            stderr=subprocess.DEVNULL,
            cwd=_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        ).decode().strip()
    except Exception:
        git_ver = "unknown"
    ctx["git_version"] = git_ver
    ctx["server_time"] = timezone.now()

    # ── Last successful snapshot ──────────────────────────────────────────────
    try:
        from .models import BrokerEquitySnapshot
        last_snap = BrokerEquitySnapshot.objects.order_by("-taken_at").first()
        ctx["last_snapshot"] = last_snap
    except Exception:
        ctx["last_snapshot"] = None

    # ── Redis health + WS counter + queue lag ─────────────────────────────────
    redis_info: dict = {}
    try:
        import redis as _redis_lib
        from django.conf import settings as _s
        _redis_url = getattr(_s, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        r = _redis_lib.from_url(_redis_url, socket_connect_timeout=2)
        r.ping()
        info = r.info("memory")
        redis_info["status"]         = "ok"
        redis_info["used_memory_mb"] = round(info["used_memory"] / 1024 / 1024, 1)
        redis_info["maxmemory_mb"]   = round(info.get("maxmemory", 0) / 1024 / 1024, 1)
        redis_info["ws_connections"] = int(r.get("trx:metrics:ws_connections") or 0)
        celery_queue_len             = r.llen("celery")
        redis_info["celery_queue"]   = celery_queue_len
        # Track peaks inline (queue depth + memory)
        try:
            from .observability import peak_update as _pu
            _pu(_redis_url, "celery_queue_lag", float(celery_queue_len))
            _pu(_redis_url, "redis_memory_mb", redis_info["used_memory_mb"])
        except Exception:
            pass
        # Recent task failures
        raw_failures = r.lrange("trx:metrics:task_failures", 0, 9)
        redis_info["task_failures"] = []
        for raw in raw_failures:
            try:
                redis_info["task_failures"].append(_json.loads(raw))
            except Exception:
                pass
    except Exception as exc:
        redis_info["status"] = "error"
        redis_info["error"]  = str(exc)
    ctx["redis"] = redis_info

    # ── Stress peaks ──────────────────────────────────────────────────────────
    try:
        from django.conf import settings as _sp
        from .observability import get_peaks
        ctx["peaks"] = get_peaks(getattr(_sp, "REDIS_URL", "") or "redis://127.0.0.1:6379/0")
    except Exception:
        ctx["peaks"] = {}

    # ── Broker monitoring (exposure + margins) ────────────────────────────────
    try:
        from .broker_monitoring import full_report
        ctx["broker_report"] = full_report()
    except Exception as exc:
        ctx["broker_report"] = {"error": str(exc)}

    # ── Operational attention: support tickets ────────────────────────────────
    try:
        _open_statuses = [SupportTicket.STATUS_OPEN, SupportTicket.STATUS_PENDING]
        ctx["open_tickets_count"] = SupportTicket.objects.filter(
            status__in=_open_statuses
        ).count()
        ctx["open_tickets"] = list(
            SupportTicket.objects
            .filter(status__in=_open_statuses)
            .order_by("-created_at")
            .values("id", "user__username", "category", "subject", "status", "priority", "created_at")[:10]
        )
    except Exception as exc:
        ctx["open_tickets_count"] = 0
        ctx["open_tickets"] = []

    # ── Operational attention: KYC pending ───────────────────────────────────
    try:
        ctx["kyc_pending_count"] = KYCProfile.objects.filter(
            status=KYCProfile.STATUS_PENDING
        ).count()
        ctx["kyc_pending"] = list(
            KYCProfile.objects
            .filter(status=KYCProfile.STATUS_PENDING)
            .order_by("-submitted_at")
            .values("id", "user__username", "user__email", "country", "submitted_at")[:10]
        )
    except Exception as exc:
        ctx["kyc_pending_count"] = 0
        ctx["kyc_pending"] = []

    # ── Operational attention: withdrawals pending/processing ─────────────────
    try:
        _wr_statuses = [WithdrawalRequest.STATUS_PENDING, WithdrawalRequest.STATUS_PROCESSING]
        ctx["withdrawals_pending_count"] = WithdrawalRequest.objects.filter(
            status__in=_wr_statuses
        ).count()
        ctx["withdrawals_pending"] = list(
            WithdrawalRequest.objects
            .filter(status__in=_wr_statuses)
            .order_by("-created_at")
            .values("id", "user__username", "amount_usd", "crypto_currency", "status", "created_at")[:10]
        )
    except Exception as exc:
        ctx["withdrawals_pending_count"] = 0
        ctx["withdrawals_pending"] = []

    # ── Operational attention: recent confirmed deposits ──────────────────────
    try:
        ctx["deposits_confirmed_count"] = Deposit.objects.filter(credited=True).count()
        ctx["deposits_recent"] = list(
            Deposit.objects
            .filter(credited=True)
            .order_by("-credited_at")
            .values("id", "user__username", "amount_usd", "crypto_currency", "credited_at")[:10]
        )
    except Exception as exc:
        ctx["deposits_confirmed_count"] = 0
        ctx["deposits_recent"] = []

    # ── New users last 7 days ─────────────────────────────────────────────────
    try:
        from datetime import timedelta as _td
        from django.contrib.auth import get_user_model as _gum
        ctx["new_users_7d"] = _gum().objects.filter(
            date_joined__gte=timezone.now() - _td(days=7)
        ).count()
    except Exception as exc:
        ctx["new_users_7d"] = 0

    # ── Stuck withdrawals ─────────────────────────────────────────────────────
    try:
        from datetime import timedelta
        stuck_cutoff = timezone.now() - timedelta(hours=48)
        ctx["stuck_withdrawals"] = list(
            WithdrawalRequest.objects
            .filter(status__in=["pending", "processing"], created_at__lte=stuck_cutoff)
            .order_by("created_at")
            .values("id", "amount_usd", "status", "created_at", "user__username")
        )
    except Exception as exc:
        ctx["stuck_withdrawals"] = []
        ctx["stuck_withdrawals_error"] = str(exc)

    # ── Recent audit events ───────────────────────────────────────────────────
    try:
        from .models import AuditLog
        ctx["recent_audit"] = list(
            AuditLog.objects
            .order_by("-created_at")
            .values("created_at", "event_type", "action", "ip", "user__username", "request_id")[:20]
        )
        ctx["recent_security"] = list(
            AuditLog.objects
            .filter(event_type__startswith="auth.")
            .order_by("-created_at")
            .values("created_at", "event_type", "action", "ip", "detail")[:10]
        )
    except Exception as exc:
        ctx["recent_audit"] = []
        ctx["recent_security"] = []

    ctx["active_section"] = "ops_panel"
    return render(request, "simulator/ops_panel.html", ctx)


# ──────────────────────────────────────────────────────────────
# 2FA — TOTP Setup + Verify views
# ──────────────────────────────────────────────────────────────

@login_required
def totp_setup_view(request):
    """
    GET  /account/2fa/setup/  — generate secret, show QR code
    POST /account/2fa/setup/  — confirm code → activate device
    """
    from .models import TOTPDevice
    from .two_factor import (
        generate_totp_secret, get_totp_uri, verify_totp_code,
        generate_qr_png, _encrypt_secret, mark_session_verified,
    )
    from .audit import log_audit, EV_ADMIN_ACTION

    # Already has confirmed device?
    existing = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    error = None

    if request.method == "POST":
        code   = request.POST.get("code", "").strip()
        secret = request.POST.get("secret", "").strip()

        if not secret or not code:
            error = "Código o secreto inválido."
        elif verify_totp_code(secret, code):
            # Save or update device
            TOTPDevice.objects.update_or_create(
                user=request.user,
                defaults={
                    "secret":       _encrypt_secret(secret),
                    "confirmed":    True,
                    "confirmed_at": timezone.now(),
                },
            )
            mark_session_verified(request)
            security_log("auth.2fa_enabled", level="info", username=request.user.username, user_id=request.user.pk)
            log_audit(request, EV_ADMIN_ACTION, f"2FA enabled for user {request.user.username}")
            return redirect("simulator:home")
        else:
            error = "Código incorrecto. Intenta de nuevo."

    # GET — generate fresh secret for setup
    raw_secret = generate_totp_secret()
    uri        = get_totp_uri(raw_secret, request.user.username)
    import base64 as _b64
    qr_b64 = _b64.b64encode(generate_qr_png(uri)).decode()

    return render(request, "simulator/totp_setup.html", {
        "existing": existing,
        "raw_secret": raw_secret,
        "qr_b64": qr_b64,
        "error": error,
    })


@login_required
def totp_verify_view(request):
    """
    GET  /account/2fa/verify/  — show code entry form
    POST /account/2fa/verify/  — verify code → set session flag
    """
    from .models import TOTPDevice
    from .two_factor import verify_totp_code, mark_session_verified, totp_session_verified

    # Already verified this session?
    if totp_session_verified(request):
        next_url = request.session.pop("2fa_next", None) or "simulator:home"
        return redirect(next_url)

    device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    if not device:
        # No device configured — pass through
        return redirect("simulator:home")

    error = None

    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        if verify_totp_code(device.secret, code):
            mark_session_verified(request)
            security_log("auth.2fa_verified", level="info", username=request.user.username, user_id=request.user.pk)
            next_url = request.session.pop("2fa_next", None) or "simulator:home"
            return redirect(next_url)
        else:
            error = "Código incorrecto."
            security_log("auth.2fa_failed", username=request.user.username, user_id=request.user.pk)

    return render(request, "simulator/totp_verify.html", {"error": error})


@login_required
def totp_disable_view(request):
    """
    POST /account/2fa/disable/  — disable 2FA after code confirmation
    """
    from .models import TOTPDevice
    from .two_factor import verify_totp_code
    from .audit import log_audit, EV_ADMIN_ACTION

    if request.method != "POST":
        return redirect("simulator:home")

    device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    if not device:
        return redirect("simulator:home")

    code = request.POST.get("code", "").strip()
    if verify_totp_code(device.secret, code):
        device.delete()
        request.session.pop("2fa_verified", None)
        security_log("auth.2fa_disabled", level="warning", username=request.user.username, user_id=request.user.pk)
        log_audit(request, EV_ADMIN_ACTION, f"2FA disabled for user {request.user.username}")
        return redirect("simulator:home")

    return render(request, "simulator/totp_setup.html", {
        "existing": device,
        "error": "Código incorrecto — 2FA no desactivado.",
    })


# ─────────────────────────────────────────────
# Broker Ecosystem Modules
# ─────────────────────────────────────────────

@login_required
def calendar_view(request):
    now     = timezone.now()
    upcoming = CalendarEvent.objects.filter(published=True, event_date__gte=now).order_by('event_date')
    past     = CalendarEvent.objects.filter(published=True, event_date__lt=now).order_by('-event_date')[:30]
    return render(request, 'simulator/calendar.html', {
        'upcoming': upcoming,
        'past':     past,
        'active_section': 'calendar',
    })


@login_required
def associates_view(request):
    ref, _ = Referral.objects.get_or_create(
        user=request.user,
        defaults={'code': _secrets.token_urlsafe(8)},
    )
    referral_url = request.build_absolute_uri(
        reverse('simulator:referral_click', args=[ref.code])
    )
    return render(request, 'simulator/associates.html', {
        'referral':     ref,
        'referral_url': referral_url,
        'active_section': 'associates',
    })


def referral_click_view(request, code):
    from django.db.models import F
    try:
        Referral.objects.filter(code=code).update(clicks=F('clicks') + 1)
    except Exception:
        pass
    return redirect('simulator:register')


@login_required
def bonuses_view(request):
    now     = timezone.now()
    bonuses = Bonus.objects.filter(active=True).order_by('-created_at')
    live    = [b for b in bonuses if not b.is_expired]
    expired = Bonus.objects.filter(active=False).order_by('-created_at')[:6]
    return render(request, 'simulator/bonuses.html', {
        'bonuses':  live,
        'expired':  expired,
        'active_section': 'bonuses',
    })


@login_required
def documents_view(request):
    docs = BrokerDocument.objects.filter(public=True)
    by_category = {}
    for cat_key, cat_label in BrokerDocument.CATEGORIES:
        cat_docs = [d for d in docs if d.category == cat_key]
        if cat_docs:
            by_category[cat_label] = cat_docs
    return render(request, 'simulator/documents.html', {
        'by_category': by_category,
        'total':       docs.count(),
        'active_section': 'documents',
    })


@login_required
def experts_view(request):
    eas = ExpertAdvisor.objects.filter(active=True).order_by('category', 'name')
    by_category = {}
    for cat_key, cat_label in ExpertAdvisor.EA_CATEGORIES:
        cat_eas = [e for e in eas if e.category == cat_key]
        if cat_eas:
            by_category[cat_label] = cat_eas
    return render(request, 'simulator/experts.html', {
        'by_category': by_category,
        'total':       eas.count(),
        'active_section': 'experts',
    })


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# User Profile
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def profile_view(request):
    """GET /profile/ — view and edit basic profile (first/last name)."""
    saved = False
    if request.method == "POST":
        form = UserProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            return redirect("simulator:profile")
    else:
        form = UserProfileForm(instance=request.user)
        saved = request.GET.get("saved") == "1"

    try:
        kyc_status = request.user.kyc_profile.status
    except KYCProfile.DoesNotExist:
        kyc_status = KYCProfile.STATUS_NOT_STARTED

    from .models import TOTPDevice as _TOTPDevice
    totp_enabled = _TOTPDevice.objects.filter(user=request.user, confirmed=True).exists()

    return render(request, "simulator/profile.html", {
        "form":           form,
        "kyc_status":     kyc_status,
        "email_verified": _is_email_verified(request.user),
        "totp_enabled":   totp_enabled,
        "active_section": "profile",
    })


# KYC — Know Your Customer verification
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def kyc_view(request):
    kyc, _ = KYCProfile.objects.get_or_create(user=request.user)

    editable = kyc.status in (KYCProfile.STATUS_NOT_STARTED, KYCProfile.STATUS_REJECTED)
    form     = None

    if editable:
        if request.method == "POST":
            form = KYCProfileForm(request.POST, request.FILES, instance=kyc)
            if form.is_valid():
                kyc            = form.save(commit=False)
                kyc.status     = KYCProfile.STATUS_PENDING
                kyc.submitted_at = timezone.now()
                kyc.reviewed_at  = None
                kyc.reviewed_by  = None
                kyc.rejection_reason = ""
                kyc.save()
                return redirect("simulator:kyc")
        else:
            form = KYCProfileForm(instance=kyc)

    return render(request, "simulator/kyc.html", {
        "kyc":            kyc,
        "form":           form,
        "editable":       editable,
        "active_section": "kyc",
    })


# ===================================================
# INTERNAL CHALLENGE STATUS API  (Phase 5G)
# ===================================================

def _check_status_token(request) -> bool:
    """Return True if the request carries a valid CHALLENGE_STATUS_API_TOKEN."""
    configured = getattr(settings, "CHALLENGE_STATUS_API_TOKEN", "").strip()
    if not configured:
        return False
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer "):].strip()
    import hmac as _hmac_tok
    return _hmac_tok.compare_digest(configured, provided)


def _enrollment_snapshot(enrollment) -> dict:
    """
    Build the read-only status payload for *enrollment*.

    Uses challenge_engine internal helpers (pure DB reads, no side effects).
    The active account depends on enrollment status:
      PHASE_1  → phase1_account
      PHASE_2  → phase2_account
      FUNDED   → funded_account
      FAILED   → last non-null account in phase order
    """
    from simulator.challenge_engine import (
        _trading_days,
        _realized_dd_pct,
        _daily_realized_dd_pct,
        _days_elapsed,
        _profit_pct,
        _phase_rules,
    )

    st       = enrollment.status
    product  = enrollment.product
    CE       = ChallengeEnrollment

    # --- resolve active account ------------------------------------------------
    if st == CE.ST_PHASE_1:
        account = enrollment.phase1_account
    elif st == CE.ST_PHASE_2:
        account = enrollment.phase2_account
    elif st == CE.ST_FUNDED:
        account = enrollment.funded_account
    else:  # FAILED / WITHDRAWN — use last known account
        account = (
            enrollment.phase2_account
            or enrollment.phase1_account
        )

    # --- is_passing / fail_reason ----------------------------------------------
    if st == CE.ST_FUNDED:
        is_passing  = True
        fail_reason = None
    elif st == CE.ST_FAILED:
        is_passing  = False
        fail_reason = enrollment.failure_reason
    else:
        is_passing  = True   # still active — optimistic until rules say otherwise
        fail_reason = None

    # --- evaluation_status (mirrors engine constants without calling evaluator) -
    if st in (CE.ST_PHASE_1, CE.ST_PHASE_2):
        evaluation_status = "IN_PROGRESS"
    elif st == CE.ST_FUNDED:
        evaluation_status = "PASSED"
    else:
        evaluation_status = "FAILED"

    # --- account-level metrics (all None-safe) ---------------------------------
    if account:
        rules              = _phase_rules(enrollment)
        balance            = float(account.balance)
        equity             = float(account.equity)
        account_size       = float(account.initial_balance or account.balance)
        profit_prog_pct    = float(_profit_pct(account))
        daily_dd_pct       = float(_daily_realized_dd_pct(account))
        max_dd_pct         = float(_realized_dd_pct(account))
        trading_days_done  = _trading_days(account)
        days_elapsed       = _days_elapsed(account)

        profit_target_pct_val  = float(rules.get("profit_target_pct", 0))
        max_drawdown_limit_pct = float(rules.get("max_drawdown_pct", 0))
        daily_loss_limit_pct   = float(rules.get("max_daily_loss_pct", 0))
        min_trading_days_req   = int(rules.get("min_trading_days", 0))
        max_duration_days      = int(rules.get("max_duration_days", 0))
        days_remaining         = max(0, max_duration_days - days_elapsed)
    else:
        balance = equity = account_size = profit_prog_pct = None
        daily_dd_pct = max_dd_pct = None
        trading_days_done = days_elapsed = 0
        profit_target_pct_val = max_drawdown_limit_pct = daily_loss_limit_pct = None
        min_trading_days_req = max_duration_days = days_remaining = None

    # --- URLs ------------------------------------------------------------------
    account_id = account.pk if account else None
    login_url   = "/login/"
    trading_url = f"/dashboard/{account_id}/" if account_id else "/dashboard/"

    return {
        "ok":                          True,
        "external_event_id":           enrollment.external_event_id,
        "product_external_code":       product.external_code,
        "product_name":                product.name,
        "enrollment_id":               enrollment.pk,
        "account_id":                  account_id,
        "account_type":                account.account_type if account else None,
        "phase":                       account.phase if account else None,
        "status":                      st,
        "balance":                     balance,
        "equity":                      equity,
        "account_size":                account_size,
        "profit_target_pct":           profit_target_pct_val,
        "profit_target_progress_pct":  profit_prog_pct,
        "daily_drawdown_pct":          daily_dd_pct,
        "max_drawdown_limit_pct":      max_drawdown_limit_pct,
        "max_drawdown_pct":            max_dd_pct,
        "daily_loss_limit_pct":        daily_loss_limit_pct,
        "min_trading_days_current":    trading_days_done,
        "min_trading_days_required":   min_trading_days_req,
        "days_remaining":              days_remaining,
        "evaluation_status":           evaluation_status,
        "is_passing":                  is_passing,
        "fail_reason":                 fail_reason,
        "failed_at_phase":             enrollment.failed_at_phase,
        "login_url":                   login_url,
        "trading_url":                 trading_url,
    }


@csrf_exempt
def challenge_status_view(request, external_event_id: str):
    """
    GET /api/internal/challenge/status/<external_event_id>/

    Read-only endpoint consumed by Vital Trader to display challenge progress.
    Requires: Authorization: Bearer <CHALLENGE_STATUS_API_TOKEN>
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    if not _check_status_token(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    enrollment = (
        ChallengeEnrollment.objects
        .select_related("product", "user", "phase1_account", "phase2_account", "funded_account")
        .filter(external_event_id=external_event_id)
        .first()
    )
    if enrollment is None:
        return JsonResponse({"error": "Enrollment not found"}, status=404)

    return JsonResponse(_enrollment_snapshot(enrollment))


# ── Email verification ────────────────────────────────────────────────────────

def verify_email_view(request, token: str):
    """
    GET /verify-email/<token>/
    Validates the signed token, marks EmailVerification.verified=True.
    Does not require login — link is opened from the user's email client.
    """
    from .email_verification import verify_email_token as _check_token

    user_pk = _check_token(token)
    context: dict = {}

    if user_pk is None:
        context["success"] = False
        context["message"] = "El enlace de verificación es inválido o ha expirado."
    else:
        try:
            from django.contrib.auth import get_user_model as _gum
            _User = _gum()
            user = _User.objects.get(pk=user_pk)
            ev, _ = EmailVerification.objects.get_or_create(user=user)
            if not ev.verified:
                ev.verified    = True
                ev.verified_at = timezone.now()
                ev.save(update_fields=["verified", "verified_at"])
                logger.info("[email_verify] user=%s email verified", user.username)
            context["success"] = True
            context["message"] = "¡Tu email ha sido verificado exitosamente!"
        except Exception as exc:
            logger.error("[email_verify] error: %s", exc)
            context["success"] = False
            context["message"] = "No se pudo completar la verificación. Intenta de nuevo."

    return render(request, "simulator/email_verify.html", context)


@login_required
def accept_terms_view(request):
    """
    GET  /legal/accept/        — show terms + risk disclaimer checkboxes.
    POST /legal/accept/        — validate, persist TermsAcceptance, redirect.

    Supports ?next=<path> to return user to their original destination.
    """
    _next = request.GET.get("next") or request.POST.get("next") or ""
    if _next and not _next.startswith("/"):
        _next = ""  # reject absolute URLs / off-site redirects

    # If already accepted, fast-forward
    if _has_accepted_terms(request.user):
        return redirect(_next or reverse("simulator:home"))

    error = None
    if request.method == "POST":
        required = ["accept_terms", "accept_risk", "accept_withdrawal_policy", "understand_risk"]
        if not all(request.POST.get(f) for f in required):
            error = "Debes marcar todas las casillas para continuar."
        else:
            TermsAcceptance.objects.get_or_create(
                user=request.user,
                terms_version=TERMS_VERSION,
                risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
                defaults={
                    "ip_address": get_client_ip(request),
                    "user_agent": request.META.get("HTTP_USER_AGENT", "")[:512],
                },
            )
            logger.info("[accept_terms] user=%s terms=%s risk=%s",
                        request.user.username, TERMS_VERSION, RISK_DISCLOSURE_VERSION)
            return redirect(_next or reverse("simulator:home"))

    return render(request, "simulator/terms_accept.html", {
        "error":          error,
        "next":           _next,
        "terms_version":  TERMS_VERSION,
        "risk_version":   RISK_DISCLOSURE_VERSION,
        "active_section": "legal",
    })


@login_required
def resend_verification_view(request):
    """POST /resend-verification/ — re-queues the verification email."""
    if request.method != "POST":
        return redirect("simulator:home")

    try:
        ev = request.user.email_verification
        if ev.verified:
            return redirect("simulator:home")
    except EmailVerification.DoesNotExist:
        EmailVerification.objects.create(user=request.user, verified=False)

    try:
        from .email_verification import make_email_token as _make_token
        from .tasks import send_email_async as _send_async
        _token = _make_token(request.user.pk)
        _verify_url = (
            settings.SITE_URL + reverse("simulator:verify_email", args=[_token])
        )
        _send_async.delay(
            subject="Verifica tu email — TRX Simulator",
            message=(
                f"Hola {request.user.username},\n\n"
                f"Verifica tu email visitando:\n{_verify_url}\n\n"
                "El enlace expira en 48 horas."
            ),
            recipient_list=[request.user.email],
        )
        logger.info("[resend_verification] sent to user=%s", request.user.username)
    except Exception as exc:
        logger.warning("[resend_verification] email failed user=%s: %s",
                       request.user.username, exc)

    return redirect("simulator:home")


# ─────────────────────────────────────────────────────────────────────────────
# Support Tickets
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def support_view(request):
    success = False
    error   = None

    if request.method == "POST":
        category = request.POST.get("category", "").strip()
        subject  = request.POST.get("subject",  "").strip()
        message  = request.POST.get("message",  "").strip()

        valid_categories = {c for c, _ in SupportTicket.CATEGORY_CHOICES}
        if not category or category not in valid_categories:
            error = "Selecciona una categoría válida."
        elif not subject:
            error = "El asunto es obligatorio."
        elif len(subject) > 200:
            error = "El asunto no puede superar los 200 caracteres."
        elif not message:
            error = "El mensaje es obligatorio."
        else:
            ticket = SupportTicket.objects.create(
                user     = request.user,
                category = category,
                subject  = subject,
                message  = message,
                status   = SupportTicket.STATUS_OPEN,
                priority = SupportTicket.PRIORITY_NORMAL,
            )
            logger.info("[support] ticket created user=%s subject=%r", request.user.username, subject)
            try:
                from .support_emails import send_support_ticket_created_email
                send_support_ticket_created_email(ticket)
            except Exception as mail_exc:
                logger.warning("[support] user email failed ticket=%d: %s", ticket.id, mail_exc)
            try:
                from .support_emails import send_support_ticket_admin_email
                send_support_ticket_admin_email(ticket)
            except Exception as mail_exc:
                logger.warning("[support] admin email failed ticket=%d: %s", ticket.id, mail_exc)
            success = True

    recent_tickets = (
        SupportTicket.objects
        .filter(user=request.user)
        .order_by("-created_at")[:10]
    )

    return render(request, "simulator/support.html", {
        "tickets":        recent_tickets,
        "category_choices": SupportTicket.CATEGORY_CHOICES,
        "success":        success,
        "error":          error,
        "active_section": "support",
    })
