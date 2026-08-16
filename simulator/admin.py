# simulator/admin.py
from decimal import Decimal

from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum, Count, Q
from django.urls import path, reverse
from django.shortcuts import redirect, render
from django.utils.timezone import now
from django.utils.html import format_html
from django.db import transaction

from .models import (
    TradingAccount, Position, Trade, LedgerEntry,
    Purchase, Deposit, WithdrawalRequest, Wallet, WalletTransaction, InternalTransfer,
    TreasuryOperationRequest,
    RiskRule, DrawdownSnapshot, TradingViolation, TraderScore,
    BrokerSnapshot, SymbolExposure, TraderClassExposure,
    AuditLog,
    CalendarEvent, Referral, Bonus, BrokerDocument, ExpertAdvisor,
    BrokerLedger, BrokerSpreadConfig, Instrument,
    BrokerEquitySnapshot, BrokerRevenueSnapshot,
    AccountProduct, ChallengeProduct, ChallengeEnrollment, FundedConfig,
    KYCProfile, SupportTicket, EmailVerification, TermsAcceptance, TOTPDevice,
    FundedPayoutRequest,
    BrokerAuditEvent,
    RoutingDecision,
    LiquidityProvider, LiquidityDecision, LiquidityLedger,
    DealingDeskDecision,
)
from . import challenge_engine
from .secure_media import broker_document_secure_widget, kyc_secure_widget
from .funded_payouts import (
    FundedPayoutAlreadyProcessed,
    InsufficientFundedBalance,
    approve_sim_payout,
    approve_internal_payout,
)
from .permissions import superuser_required_action


# ─────────────────────────────────────────────
# Color helpers
# ─────────────────────────────────────────────

_STATUS_COLORS = {
    "Activo":     ("#1a472a", "#27ae60"),   # bg, fg
    "Suspendido": ("#4a1a00", "#e67e22"),
    "Violado":    ("#4a0000", "#e74c3c"),
    "Cerrado":    ("#1a1a1a", "#888888"),
    "Completado": ("#0a2a4a", "#3498db"),
}

_CLASS_COLORS = {
    "ELITE":      ("#0a2a1a", "#00e676"),
    "CONSISTENT": ("#0a2218", "#26a69a"),
    "NORMAL":     ("#1a1a2a", "#7986cb"),
    "RISKY":      ("#2a1a00", "#ffa726"),
    "MARTINGALE": ("#2a0a00", "#ff7043"),
    "TOXIC":      ("#2a0000", "#ef5350"),
    "GAMBLER":    ("#2a1f00", "#f1c40f"),
    "SCALPER":    ("#001a2a", "#29b6f6"),
}

_VIOLATION_COLORS = {
    "MAX_DRAWDOWN":       "#e74c3c",
    "MAX_DAILY_LOSS":     "#e67e22",
    "MAX_LOT_SIZE":       "#f1c40f",
    "MAX_EXPOSURE":       "#e74c3c",
    "RATE_LIMITED":       "#3498db",
    "MARTINGALE_PATTERN": "#ff7043",
}


def _badge(text, bg, fg):
    return format_html(
        '<span style="background:{};color:{};padding:2px 10px;border-radius:12px;'
        'font-size:11px;font-weight:700;white-space:nowrap">{}</span>',
        bg, fg, text,
    )


# ─────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────

class PositionInline(admin.TabularInline):
    model = Position
    extra = 0
    fields = ("symbol", "side", "qty", "avg_price", "sl", "tp", "external_id", "opened_at")
    readonly_fields = ("opened_at",)
    show_change_link = True


class TradeInline(admin.TabularInline):
    model = Trade
    extra = 0
    fields = (
        "symbol", "trade_type", "lot_size",
        "entry_price", "exit_price", "profit_loss", "opened_at", "closed_at",
    )
    readonly_fields = ("opened_at", "closed_at")
    show_change_link = True
    ordering = ("-closed_at",)

    def get_queryset(self, request):
        # Do NOT slice here — Django needs to add .filter(account=parent) after
        return super().get_queryset(request).order_by("-closed_at")


class RiskRuleInline(admin.StackedInline):
    model = RiskRule
    extra = 0
    can_delete = False
    verbose_name = "Risk Rule"
    verbose_name_plural = "Risk Rule"
    fields = (
        ("max_daily_loss_pct", "max_drawdown_pct"),
        ("max_lot_size", "max_open_positions"),
        "max_exposure_usd",
    )


class ViolationInline(admin.TabularInline):
    model = TradingViolation
    extra = 0
    can_delete = False
    readonly_fields = ("violation_type", "value_at_violation", "limit_value", "created_at", "meta")
    fields = ("violation_type", "value_at_violation", "limit_value", "created_at")
    ordering = ("-created_at",)
    max_num = 10
    verbose_name = "Recent Violation"
    verbose_name_plural = "Recent Violations (last 10)"

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("-created_at")


class DrawdownSnapshotInline(admin.TabularInline):
    model = DrawdownSnapshot
    extra = 0
    can_delete = False
    readonly_fields = ("date", "balance_start", "balance_end", "daily_pnl",
                       "daily_pnl_pct", "peak_balance", "drawdown_from_peak")
    fields = ("date", "balance_start", "balance_end", "daily_pnl",
              "daily_pnl_pct", "drawdown_from_peak")
    ordering = ("-date",)
    max_num = 0
    verbose_name = "Drawdown Snapshot"
    verbose_name_plural = "Drawdown History (last 14 days)"

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("-date")


class TraderIntelligenceInline(admin.StackedInline):
    model = TraderScore
    extra = 0
    can_delete = False
    verbose_name = "Trader Intelligence"
    verbose_name_plural = "Trader Intelligence"
    readonly_fields = ("intelligence_panel",)
    fields = ("intelligence_panel",)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="")
    def intelligence_panel(self, obj):
        if not obj or not obj.pk:
            return format_html(
                '<p style="color:#55556a;font-style:italic;padding:8px 0">'
                'Sin datos — se calcula al cerrar el primer trade.</p>'
            )
        from django.utils.html import mark_safe

        _CLS_BG = {
            "ELITE": "#0a2a1a", "CONSISTENT": "#0a2218", "NORMAL": "#1a1a2a",
            "GAMBLER": "#2a1f00", "MARTINGALE": "#2a0a00", "RISKY": "#2a1a00",
            "SCALPER": "#001a2a", "TOXIC": "#2a0000",
        }
        _CLS_FG = {
            "ELITE": "#00e676", "CONSISTENT": "#26a69a", "NORMAL": "#7986cb",
            "GAMBLER": "#f1c40f", "MARTINGALE": "#ff7043", "RISKY": "#ffa726",
            "SCALPER": "#29b6f6", "TOXIC": "#ef5350",
        }
        _RT_BG = {
            "ELITE": "#0a2a1a", "INTERNAL": "#1a1a2a",
            "REVIEW": "#2a1a00", "HEDGE_CANDIDATE": "#2a0000",
        }
        _RT_FG = {
            "ELITE": "#00e676", "INTERNAL": "#7986cb",
            "REVIEW": "#ffa726", "HEDGE_CANDIDATE": "#ef5350",
        }

        cls     = obj.trader_class
        routing = obj.routing_profile
        cls_bg  = _CLS_BG.get(cls, "#1a1a2a")
        cls_fg  = _CLS_FG.get(cls, "#aaa")
        rt_bg   = _RT_BG.get(routing, "#1a1a2a")
        rt_fg   = _RT_FG.get(routing, "#aaa")

        # Numeric values
        win_rate    = float(obj.win_rate    or 0)
        pf          = float(obj.profit_factor or 0)
        consistency = float(obj.consistency_score or 0)
        toxicity    = float(obj.toxicity_score or 0)
        gambler     = float(obj.gambler_score  or 0)
        martingale  = float(obj.martingale_rate or 0) * 100
        scalping    = float(obj.scalping_ratio  or 0) * 100
        hold_s      = float(obj.avg_hold_time_seconds or 0)
        freq        = float(obj.trade_frequency or 0)
        avg_rr      = float(obj.avg_rr          or 0)
        pnl_vol     = float(obj.pnl_volatility  or 0)
        lot_growth  = float(obj.lot_growth_rate or 0)
        cons_l      = obj.max_consecutive_losses
        cons_w      = obj.max_consecutive_wins
        avg_lot     = float(obj.avg_lot_size or 0)
        last_eval   = obj.last_evaluated.strftime("%Y-%m-%d %H:%i") if obj.last_evaluated else "—"
        if obj.last_evaluated:
            last_eval = obj.last_evaluated.strftime("%Y-%m-%d %H:%M")

        hold_str = (f"{hold_s/3600:.1f}h" if hold_s >= 3600
                    else f"{hold_s/60:.1f}m" if hold_s >= 60
                    else f"{hold_s:.0f}s")

        def _color3(v, hi, mid, hi_c, mid_c, lo_c):
            return hi_c if v >= hi else mid_c if v >= mid else lo_c

        tox_c   = _color3(toxicity,  70, 40, "#ef5350", "#e67e22", "#27ae60")
        gam_c   = _color3(gambler,   60, 30, "#f1c40f", "#e67e22", "#27ae60")
        con_c   = _color3(consistency, 60, 40, "#27ae60", "#e67e22", "#ef5350")
        wr_c    = _color3(win_rate,  55, 40, "#27ae60", "#e67e22", "#ef5350")
        pf_c    = _color3(pf,        1.5, 1.0, "#27ae60", "#e67e22", "#ef5350")
        rr_c    = _color3(avg_rr,    1.5, 1.0, "#27ae60", "#e67e22", "#ef5350")
        mart_c  = "#ef5350" if martingale >= 25 else "#e67e22" if martingale >= 10 else "#27ae60"
        freq_c  = "#ef5350" if freq >= 20 else "#e67e22" if freq >= 10 else "#27ae60"
        scal_c  = "#e67e22" if scalping >= 60 else "#27ae60"
        cl_c    = "#ef5350" if cons_l >= 5 else "#e67e22" if cons_l >= 3 else "#27ae60"
        cw_c    = "#27ae60" if cons_w >= 5 else "#e67e22"

        def _kv(label, val, color="#c8ccd8"):
            return (f'<div style="display:flex;justify-content:space-between;padding:4px 0;'
                    f'border-bottom:1px solid rgba(255,255,255,.03);">'
                    f'<span style="font-size:11px;color:#55556a;">{label}</span>'
                    f'<span style="font-size:12px;font-weight:700;color:{color};">{val}</span>'
                    f'</div>')

        def _score_card(icon, label, val, color, pct):
            pct = min(max(pct, 0), 100)
            return (f'<div style="background:#13131f;border:1px solid rgba(255,255,255,.06);'
                    f'border-top:2px solid {color};border-radius:6px;padding:10px 12px;">'
                    f'<div style="font-size:10px;color:#55556a;text-transform:uppercase;'
                    f'letter-spacing:.08em;margin-bottom:4px;">{icon} {label}</div>'
                    f'<div style="font-size:1.5rem;font-weight:800;color:{color};">{val:.0f}</div>'
                    f'<div style="background:rgba(255,255,255,.05);border-radius:3px;height:4px;margin-top:6px;">'
                    f'<div style="width:{pct:.0f}%;height:4px;border-radius:3px;background:{color};"></div>'
                    f'</div></div>')

        perf_html = (
            _kv("Win Rate",      f"{win_rate:.1f}%",   wr_c)
            + _kv("Profit Factor", f"{pf:.2f}",          pf_c)
            + _kv("Avg RR",        f"{avg_rr:.2f}",      rr_c)
            + _kv("Avg Lot Size",  f"{avg_lot:.4f}",     "#c8ccd8")
            + _kv("PnL Volatility",f"{pnl_vol:.3f}",     "#c8ccd8")
        )
        beh_html = (
            _kv("Hold Time",        hold_str,             "#c8ccd8")
            + _kv("Scalping Ratio",   f"{scalping:.1f}%",   scal_c)
            + _kv("Martingale Rate",  f"{martingale:.1f}%",  mart_c)
            + _kv("Trade Freq/día",   f"{freq:.1f}",         freq_c)
            + _kv("Lot Growth Rate",  f"{lot_growth:+.3f}",  "#c8ccd8")
            + _kv("Racha Ganancias",  str(cons_w),           cw_c)
            + _kv("Racha Pérdidas",   str(cons_l),           cl_c)
        )

        html = (
            '<div style="background:#0f0f1c;border:1px solid #1e1e30;border-radius:8px;padding:16px;">'

            # Header badges
            '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px;">'
            f'<span style="background:{cls_bg};color:{cls_fg};padding:5px 18px;border-radius:20px;'
            f'font-size:13px;font-weight:800;letter-spacing:.06em;">{cls}</span>'
            f'<span style="background:{rt_bg};color:{rt_fg};padding:3px 12px;border-radius:12px;'
            f'font-size:11px;font-weight:700;">{routing}</span>'
            f'<span style="color:#55556a;font-size:11px;margin-left:auto;">eval {last_eval}</span>'
            '</div>'

            # 3 score cards
            '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px;">'
            + _score_card("☠", "Toxicity",    toxicity,    tox_c, toxicity)
            + _score_card("🎰", "Gambler",    gambler,     gam_c, gambler)
            + _score_card("📊", "Consistency", consistency, con_c, consistency)
            + '</div>'

            # Two-column metrics
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">'

            '<div>'
            '<div style="font-size:10px;color:#55556a;text-transform:uppercase;letter-spacing:.08em;'
            'margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #1e1e30;">Performance</div>'
            + perf_html +
            '</div>'

            '<div>'
            '<div style="font-size:10px;color:#55556a;text-transform:uppercase;letter-spacing:.08em;'
            'margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #1e1e30;">Behavioral Signals</div>'
            + beh_html +
            '</div>'

            '</div></div>'
        )
        return mark_safe(html)


# ─────────────────────────────────────────────
# Admin actions
# ─────────────────────────────────────────────

_ACCOUNT_TYPE_COLORS = {
    "CHALLENGE": ("#3d1a00", "#e67e22"),
    "FUNDED":    ("#0a2a1a", "#27ae60"),
    "RETAIL":    ("#0a1a2a", "#3498db"),
}

_TIER_INITIAL = {"10K": 10000, "50K": 50000, "100K": 100000}


@admin.action(description="Resetear balance al valor inicial")
def reset_balance(modeladmin, request, queryset):
    updated = 0
    for acc in queryset:
        base = acc.initial_balance or _TIER_INITIAL.get(getattr(acc, "tier", "10K"), 10000)
        acc.balance = base
        acc.equity = base
        acc.peak_balance = base
        acc.save(update_fields=["balance", "equity", "peak_balance"])
        updated += 1
    modeladmin.message_user(request, f"{updated} cuenta(s) reseteadas.")


reset_balance = superuser_required_action(reset_balance)


@admin.action(description="Suspender cuentas seleccionadas")
def suspend_accounts(modeladmin, request, queryset):
    rows = queryset.update(status="Suspendido")
    modeladmin.message_user(request, f"{rows} cuenta(s) suspendidas.")


@admin.action(description="Reactivar cuentas seleccionadas")
def activate_accounts(modeladmin, request, queryset):
    rows = queryset.update(status="Activo")
    modeladmin.message_user(request, f"{rows} cuenta(s) reactivadas.")


@admin.action(description="Activar NETTING (consolidar por símbolo/side)")
def enable_netting(modeladmin, request, queryset):
    rows = queryset.update(netting_mode=True)
    modeladmin.message_user(request, f"{rows} cuenta(s) con NETTING activado.")


@admin.action(description="Desactivar NETTING → HEDGING")
def disable_netting(modeladmin, request, queryset):
    rows = queryset.update(netting_mode=False)
    modeladmin.message_user(request, f"{rows} cuenta(s) en modo HEDGING.")


@admin.action(description="Recalcular Risk Rule (aplicar defaults del tier)")
def recalc_risk_rules(modeladmin, request, queryset):
    from .risk_engine import get_or_create_risk_rule
    for acc in queryset:
        get_or_create_risk_rule(acc)
    modeladmin.message_user(request, f"Risk rules verificadas/creadas para {queryset.count()} cuenta(s).")


@admin.action(description="Recalcular Trader Intelligence (score + routing)")
def recalc_trader_scores(modeladmin, request, queryset):
    from .intelligence_engine import update_intelligence
    count = 0
    for obj in queryset:
        account = obj.account if isinstance(obj, TraderScore) else obj
        update_intelligence(account)
        count += 1
    modeladmin.message_user(request, f"Intelligence actualizado para {count} cuenta(s).")


# ─────────────────────────────────────────────
# TradingAccount
# ─────────────────────────────────────────────

@admin.register(TradingAccount)
class TradingAccountAdmin(admin.ModelAdmin):

    # ── Computed display columns ──

    @admin.display(description="Type", ordering="account_type")
    def account_type_badge(self, obj):
        bg, fg = _ACCOUNT_TYPE_COLORS.get(obj.account_type, ("#1a1a1a", "#aaa"))
        return _badge(obj.account_type, bg, fg)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        bg, fg = _STATUS_COLORS.get(obj.status, ("#1a1a1a", "#aaa"))
        return _badge(obj.status, bg, fg)

    @admin.display(description="Total DD %")
    def total_dd_pct(self, obj):
        if not obj.peak_balance or obj.peak_balance == 0:
            return "—"
        pct = float((obj.peak_balance - obj.balance) / obj.peak_balance * 100)
        color = "#e74c3c" if pct >= 7 else "#e67e22" if pct >= 3 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{pct:.2f}%")

    @admin.display(description="Peak Balance")
    def peak_balance_display(self, obj):
        return f"${float(obj.peak_balance):,.2f}"

    @admin.display(description="Retail Margin Engine")
    def margin_panel(self, obj):
        if not obj or obj.account_type != "RETAIL":
            return "Solo visible para cuentas RETAIL."
        from .models import Position
        from .risk_engine import compute_margin_state, _MARGIN_THRESHOLDS
        from django.utils.html import mark_safe

        lev = max(1, obj.leverage or 50)
        positions = list(Position.objects.filter(account=obj))
        total_margin = sum(float(p.avg_price) * float(p.qty) / lev for p in positions)
        equity = float(obj.equity or obj.balance or 0)
        balance = float(obj.balance or 0)
        mg = compute_margin_state(equity, total_margin)

        used_pct = mg["used_margin_pct"]
        mlevel = mg["margin_level"]
        if used_pct >= _MARGIN_THRESHOLDS["DANGER"]:
            bar_color, status_label = "#e74c3c", "DANGER"
        elif used_pct >= _MARGIN_THRESHOLDS["HIGH"]:
            bar_color, status_label = "#e67e22", "HIGH RISK"
        elif used_pct >= _MARGIN_THRESHOLDS["WARNING"]:
            bar_color, status_label = "#f1c40f", "WARNING"
        else:
            bar_color, status_label = "#27ae60", "NORMAL"

        bar_w = min(int(used_pct), 100)
        ml_color = ("#e74c3c" if (mlevel > 0 and mlevel < 100)
                    else "#e67e22" if (mlevel > 0 and mlevel < 150)
                    else "#27ae60")

        def _kv(label, val, color="#c8ccd8"):
            return (f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
                    f'border-bottom:1px solid rgba(255,255,255,.04);">'
                    f'<span style="font-size:11px;color:#55556a;">{label}</span>'
                    f'<span style="font-size:12px;font-weight:700;color:{color};">{val}</span></div>')

        rows = (
            _kv("Balance",       f"${balance:,.2f}")
            + _kv("Equity",        f"${equity:,.2f}")
            + _kv("Margin Used",   f"${mg['margin_used']:,.2f}")
            + _kv("Free Margin",   f"${mg['free_margin']:,.2f}",
                  "#27ae60" if mg["free_margin"] >= 0 else "#e74c3c")
            + _kv("Used Margin %", f"{used_pct:.2f}%", bar_color)
            + _kv("Margin Level",
                  f"{mlevel:.0f}%" if mg["margin_used"] > 0 else "—",
                  ml_color if mg["margin_used"] > 0 else "#55556a")
            + _kv("Maintenance Req.", f"${mg['maintenance_margin']:,.2f}")
            + _kv("Liq. Distance",
                  f"${mg['liquidation_distance']:,.2f}",
                  "#27ae60" if mg["liquidation_distance"] > balance * 0.1 else "#e74c3c")
            + _kv("Open Positions", str(len(positions)))
            + _kv("Leverage", f"1:{lev}")
        )

        html = (
            '<div style="background:#0f0f1c;border:1px solid #1e1e30;border-radius:8px;padding:16px;">'
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">'
            '<span style="background:#0a1a2a;color:#3498db;padding:3px 12px;border-radius:12px;'
            'font-size:11px;font-weight:700;">RETAIL MARGIN ENGINE</span>'
            f'<span style="padding:3px 10px;border-radius:8px;font-size:11px;font-weight:800;'
            f'color:{bar_color};background:rgba(0,0,0,.3);border:1px solid {bar_color}33;">'
            f'{status_label}</span>'
            '</div>'
            '<div style="margin-bottom:14px;">'
            '<div style="display:flex;justify-content:space-between;margin-bottom:6px;">'
            '<span style="font-size:10px;color:#55556a;text-transform:uppercase;letter-spacing:.08em;">'
            'Margin Utilization</span>'
            f'<span style="font-size:14px;font-weight:800;color:{bar_color};">{used_pct:.2f}%</span>'
            '</div>'
            '<div style="background:rgba(255,255,255,.05);border-radius:4px;height:10px;overflow:hidden;">'
            f'<div style="width:{bar_w}%;height:10px;border-radius:4px;background:{bar_color};"></div>'
            '</div>'
            '<div style="display:flex;justify-content:space-between;margin-top:4px;">'
            '<span style="font-size:9px;color:#55556a;">0%  NORMAL</span>'
            '<span style="font-size:9px;color:#f1c40f;">20% WARN</span>'
            '<span style="font-size:9px;color:#e67e22;">50% HIGH</span>'
            '<span style="font-size:9px;color:#e74c3c;">80% DANGER</span>'
            '</div></div>'
            + rows +
            '</div>'
        )
        return mark_safe(html)

    @admin.display(description="Desk")
    def dealing_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[obj.pk])
        return format_html('<a class="button" href="{}">→ Desk</a>', url)

    class Media:
        js = ("simulator/admin/account_type_toggle.js",)

    # ── List config ──

    list_display = (
        "id", "user", "account_type_badge", "tier", "phase",
        "balance", "equity", "peak_balance_display",
        "total_dd_pct", "open_positions", "violations_count",
        "status_badge", "trader_class_badge",
        "leverage", "netting_mode", "created_at",
        "dealing_link",
    )
    list_filter  = ("account_type", "tier", "phase", "status", "netting_mode", "created_at")
    search_fields = ("user__username", "user__email")
    # On change forms: peak_balance and drawdown are read-only computed values.
    # On add forms they are excluded entirely (save() auto-derives them from balance).
    readonly_fields = ("created_at", "peak_balance", "drawdown", "margin_panel")

    # ── Add form: only the fields the operator actually needs to fill in ──
    _ADD_FIELDSETS = (
        ("Cuenta", {
            "fields": ("user", "account_type", "status"),
        }),
        ("Balance inicial", {
            "description": (
                "Introduce solo el balance. "
                "equity y peak_balance se calculan automáticamente al guardar."
            ),
            "fields": ("initial_balance",),
        }),
        ("Configuración", {
            "fields": ("leverage", "currency", "netting_mode"),
        }),
        ("Challenge / Funded", {
            "classes": ("challenge-section", "collapse"),
            "description": "Solo para cuentas Challenge y Funded.",
            "fields": ("tier", "phase", "profit_target", "max_drawdown"),
        }),
    )

    # ── Change form: full view including computed fields ──────────────────
    _CHANGE_FIELDSETS = (
        ("Cuenta", {
            "fields": ("user", "account_type", "status"),
        }),
        ("Balances", {
            "fields": ("initial_balance", "balance", "equity", "peak_balance", "drawdown"),
        }),
        ("Configuración", {
            "fields": ("leverage", "currency", "netting_mode"),
        }),
        ("Challenge / Funded", {
            "classes": ("challenge-section", "collapse"),
            "description": "Estos campos aplican solo a cuentas Challenge y Funded.",
            "fields": ("tier", "phase", "profit_target", "max_drawdown"),
        }),
        ("Retail — Margin Engine", {
            "description": "Estado en tiempo real del motor de margen. Solo para cuentas RETAIL.",
            "fields": ("margin_panel",),
        }),
        ("Metadatos", {
            "classes": ("collapse",),
            "fields": ("created_at",),
        }),
    )

    # fieldsets required by ModelAdmin (used as default; overridden by get_fieldsets)
    fieldsets = _CHANGE_FIELDSETS

    def get_fieldsets(self, request, obj=None):
        return self._ADD_FIELDSETS if obj is None else self._CHANGE_FIELDSETS

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ("created_at",)
        base = self.readonly_fields
        if not request.user.is_superuser:
            base = base + ("balance", "equity", "initial_balance")
        return base

    inlines = [RiskRuleInline, TraderIntelligenceInline, ViolationInline, DrawdownSnapshotInline, PositionInline, TradeInline]
    actions = [reset_balance, suspend_accounts, activate_accounts, enable_netting, disable_netting,
               recalc_risk_rules, recalc_trader_scores]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            _violations_count=Count("violations"),
            _open_positions=Count("positions"),
        ).prefetch_related("trader_score")
        return qs

    @admin.display(description="Violations")
    def violations_count(self, obj):
        n = obj._violations_count
        if n == 0:
            return "—"
        color = "#e74c3c" if n >= 3 else "#e67e22"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, n)

    @admin.display(description="Open Pos.")
    def open_positions(self, obj):
        n = obj._open_positions
        if n == 0:
            return "—"
        return format_html('<span style="color:#3498db;font-weight:700">{}</span>', n)

    @admin.display(description="Trader Class")
    def trader_class_badge(self, obj):
        try:
            score = obj.trader_score
        except Exception:
            return "—"
        bg, fg = _CLASS_COLORS.get(score.trader_class, ("#1a1a2a", "#aaa"))
        return _badge(score.trader_class, bg, fg)

    # ── Dealing Desk custom view ──

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:account_id>/dealing-desk/",
                self.admin_site.admin_view(self.dealing_desk_view),
                name="simulator_tradingaccount_dealing_desk",
            ),
        ]
        return custom + urls

    def dealing_desk_view(self, request, account_id: int):
        from decimal import Decimal
        account = TradingAccount.objects.filter(pk=account_id).first()
        if not account:
            messages.error(request, "Cuenta no encontrada.")
            return redirect("admin:simulator_tradingaccount_changelist")

        # ── Quick actions (POST) ──
        if request.method == "POST":
            desk_action = request.POST.get("action", "")

            if desk_action == "suspend":
                account.status = "Suspendido"
                account.save(update_fields=["status"])
                messages.warning(request, f"Cuenta #{account_id} suspendida.")
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

            if desk_action == "activate":
                account.status = "Activo"
                account.save(update_fields=["status"])
                messages.success(request, f"Cuenta #{account_id} reactivada.")
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

            if desk_action == "reset":
                if not request.user.is_superuser:
                    messages.error(request, "Permiso denegado — esta acción requiere superusuario.")
                    return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)
                base = account.initial_balance or Decimal(str(_TIER_INITIAL.get(account.tier, 10000)))
                account.balance = base
                account.equity = base
                account.peak_balance = base
                account.status = "Activo"
                account.save(update_fields=["balance", "equity", "peak_balance", "status"])
                messages.success(request, f"Cuenta #{account_id} reseteada a ${base:,.0f}.")
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

            if desk_action == "recalc_score":
                from .intelligence_engine import update_intelligence
                update_intelligence(account)
                messages.success(request, "Trader intelligence recalculado.")
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

            if desk_action == "recalc_risk":
                from .risk_engine import get_or_create_risk_rule
                get_or_create_risk_rule(account)
                messages.success(request, "Risk rule verificada/creada.")
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

            if desk_action == "force_close":
                if not request.user.is_superuser:
                    messages.error(request, "Permiso denegado — esta acción requiere superusuario.")
                    return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)
                from django.db import transaction as db_tx
                symbol = (request.POST.get("symbol") or "").strip()
                try:
                    px = float(request.POST.get("price")) if request.POST.get("price") else None
                except Exception:
                    px = None
                total_closed, total_pnl = 0, 0.0
                with db_tx.atomic():
                    # PANEL-02 INVARIANTE-2 — global lock order is
                    # TradingAccount → Position (see the "global
                    # Position/TradingAccount lock order" note in
                    # consumers.py). Lock the account FIRST — this is the
                    # account's real mutex regardless of how many
                    # positions currently exist — THEN lock the matching
                    # Position rows, so the list below is guaranteed fresh
                    # (reflects every write any concurrent WS/daemon
                    # transaction for this SAME account already
                    # committed), never a pre-lock/stale snapshot.
                    locked_account = (
                        TradingAccount.objects.select_for_update().filter(pk=account.pk).first()
                    )
                    running_balance = float(locked_account.balance) if locked_account else float(account.balance or 0)

                    # .order_by("id") keeps multi-row lock acquisition
                    # order deterministic (defensive — with Account as the
                    # outer mutex, no two transactions ever hold
                    # overlapping Position locks for this account
                    # simultaneously, but this remains correct if that
                    # ever changes).
                    qs = Position.objects.select_for_update().filter(account=locked_account).order_by("id")
                    if symbol:
                        qs = qs.filter(symbol=symbol)
                    positions = list(qs)
                    account_currency = getattr(locked_account or account, "currency", "USD") or "USD"

                    for pos in positions:
                        exit_px = px if px is not None else float(pos.avg_price)
                        # MARGIN-02 — was missing BOTH contract_size and
                        # currency conversion; delegates to pnl_engine now,
                        # same as every other real close path.
                        from .pnl_engine import position_pnl_float
                        pnl = position_pnl_float(
                            pos.side, float(pos.avg_price), exit_px, float(pos.qty), pos.symbol,
                            account_currency=account_currency,
                        )
                        force_close_trade = Trade.objects.create(
                            account=locked_account or account, symbol=pos.symbol,
                            trade_type=pos.side,          # record original side, consistent with WS consumer
                            lot_size=pos.qty, entry_price=pos.avg_price,
                            exit_price=Decimal(str(exit_px)), stop_loss=pos.sl,
                            take_profit=pos.tp, profit_loss=Decimal(str(pnl)),
                            opened_at=pos.opened_at, closed_at=now(),
                            # BOOK-04c — verbatim copy of the PRINCIPAL
                            # decision, read from the still-locked `pos`
                            # before pos.delete() later in this same loop
                            # iteration. NULL propagates honestly.
                            routing_decision=pos.routing_decision,
                        )
                        running_balance += pnl
                        LedgerEntry.objects.create(
                            account=locked_account or account, event_type=LedgerEntry.EV_REALIZED,
                            amount=Decimal(str(pnl)), balance_after=Decimal(str(running_balance)),
                            meta={"reason": "admin_force_close", "symbol": pos.symbol},
                        )
                        # BOOK-02 — broker's B-Book counterparty result for
                        # this same Trade, same transaction. This writes
                        # exactly one canonical FINANCIAL audit event
                        # (EV_POSITION_CLOSED_ADMIN) via broker_ledger.py's
                        # single writer — it has no access to a staff
                        # actor and does not try to record one.
                        from .broker_ledger import create_broker_counterparty_entry
                        create_broker_counterparty_entry(
                            force_close_trade, locked_account or account, pnl, "admin_force_close",
                        )
                        # BOOK-05d.3c — Liquidity Ledger. Purely observational,
                        # simulated — never affects Trade, LedgerEntry,
                        # BrokerLedger, BrokerAuditEvent, pos.delete(),
                        # running_balance, total_closed/total_pnl, or the
                        # admin success/info message below. Only runs if this
                        # Trade's principal RoutingDecision (copied verbatim
                        # onto force_close_trade.routing_decision at creation,
                        # above, from pos.routing_decision) has a
                        # LiquidityDecision associated with it. Mirrors
                        # TradingConsumer._db_close_position_atomic (3a) and
                        # tasks._close_position_sync (3b) exactly, adapted to
                        # this view's own transaction alias (db_tx) and run
                        # once per position inside this loop — a failure here
                        # must never abort the remaining positions in this
                        # same queryset, which is why the nested atomic below
                        # is a per-iteration savepoint, not a one-time guard.
                        #
                        # Two independent nested savepoints, not one:
                        #   - the one below protects the LiquidityDecision
                        #     lookup for THIS position only — a DatabaseError
                        #     here must never leave the outer db_tx.atomic()
                        #     (opened once for the whole queryset, above)
                        #     marked as needing a rollback, which would abort
                        #     every other position still pending in this loop;
                        #   - record_liquidity_ledger_entry()'s own internal
                        #     transaction.atomic() (BOOK-05d.2) separately
                        #     protects just its .create() call.
                        # The except below is deliberately OUTSIDE this
                        # savepoint — catching inside it would risk leaving
                        # the savepoint itself in a broken state; catching
                        # outside it, after the `with` has already unwound,
                        # is what guarantees this iteration continues normally
                        # to pos.delete() and that the loop proceeds to the
                        # next position, regardless of what happened here.
                        # The writer's return value is never checked.
                        # BOOK-05e.3c — the two ids below (liquidity_decision's
                        # own decision_id and RoutingDecision's own decision_id)
                        # are resolved HERE, still inside this same pre-existing
                        # per-iteration savepoint, precisely so the extra
                        # RoutingDecision lookup this block adds is covered by
                        # it too — a raw ORM query's DatabaseError corrupts the
                        # surrounding transaction's DB-level state even if a
                        # bare try/except catches it; only a savepoint recovers
                        # cleanly. Resolving it here reuses the savepoint
                        # BOOK-05d.3c already opened for the LiquidityDecision
                        # lookup, rather than requiring a second, new one — "no
                        # atomic() adicional" is satisfied by scope, not by
                        # skipping protection. Mirrors
                        # TradingConsumer._db_close_position_atomic (BOOK-05e.3a)
                        # and tasks._close_position_sync (BOOK-05e.3b) exactly.
                        _liquidity_ledger_entry = None
                        _liquidity_decision_uuid = None
                        _routing_decision_uuid = None
                        if force_close_trade.routing_decision_id is not None:
                            try:
                                with db_tx.atomic():
                                    liquidity_decision = (
                                        LiquidityDecision.objects
                                        .filter(routing_decision_id=force_close_trade.routing_decision_id)
                                        .order_by("-decided_at")
                                        .first()
                                    )

                                    if liquidity_decision is not None:
                                        from .liquidity_ledger import record_liquidity_ledger_entry
                                        _liquidity_ledger_entry = record_liquidity_ledger_entry(
                                            source_trade_id=force_close_trade.id,
                                            liquidity_decision_id=liquidity_decision.id,
                                            symbol=force_close_trade.symbol,
                                            simulated_pnl=(
                                                Decimal("0.00") if force_close_trade.profit_loss == 0
                                                else -force_close_trade.profit_loss
                                            ),
                                            meta={
                                                "trader_pnl": float(force_close_trade.profit_loss),
                                                "close_reason": "admin_force_close",
                                            },
                                        )
                                        if _liquidity_ledger_entry is not None:
                                            _liquidity_decision_uuid = liquidity_decision.decision_id
                                            _routing_decision_uuid = (
                                                RoutingDecision.objects
                                                .filter(pk=force_close_trade.routing_decision_id)
                                                .values_list("decision_id", flat=True)
                                                .first()
                                            )
                            except Exception as _liquidity_ledger_exc:
                                import logging
                                logger = logging.getLogger("simulator.admin")
                                logger.warning(
                                    "[liquidity_ledger] entry failed trade=%s: %s",
                                    force_close_trade.id, _liquidity_ledger_exc, exc_info=True,
                                )

                        # BOOK-05e.3c — Liquidity Ledger audit trail. A second,
                        # independent try/except from the write above (same
                        # rationale BOOK-05e.3a/3b already established:
                        # catching an audit-event failure inside the writer's
                        # own except would misattribute it as a
                        # "liquidity_ledger entry failed" instead of what it
                        # really is). Deliberately placed AFTER the nested
                        # `with db_tx.atomic()` above has already closed —
                        # never inside it — so that a failure constructing or
                        # sending this event can never roll back the
                        # LiquidityLedger row already committed a moment
                        # earlier, relative to its own savepoint, and can
                        # never abort the remaining positions in this same
                        # queryset loop. Still runs inside the outer
                        # db_tx.atomic() opened once for the whole queryset —
                        # no additional atomic() needed here:
                        # record_liquidity_event() (-> record_event()) already
                        # opens and fully contains its own internal savepoint
                        # and never raises. Only runs when the writer above
                        # actually produced a row for THIS position. Return
                        # value ignored, same as every other record_*_event()
                        # call site.
                        if _liquidity_ledger_entry is not None:
                            try:
                                from . import broker_audit as _audit
                                _audit.record_liquidity_event(
                                    event_type=_audit.EV_LIQUIDITY_LEDGER_RECORDED,
                                    description=(
                                        f"Liquidity ledger entry recorded for {force_close_trade.symbol} "
                                        f"(trade_id={force_close_trade.id})"
                                    ),
                                    account=locked_account or account,
                                    trade_id=force_close_trade.id,
                                    symbol=force_close_trade.symbol,
                                    source_module="simulator.admin",
                                    metadata={
                                        "liquidity_ledger_id": _liquidity_ledger_entry.id,
                                        "liquidity_decision_id": str(_liquidity_decision_uuid),
                                        "routing_decision_id": (
                                            str(_routing_decision_uuid) if _routing_decision_uuid else None
                                        ),
                                        "position_id": pos.id,
                                        "close_reason": "admin_force_close",
                                    },
                                )
                            except Exception as _liquidity_ledger_audit_exc:
                                import logging
                                logger = logging.getLogger("simulator.admin")
                                logger.warning(
                                    "[liquidity_ledger] audit event failed trade=%s: %s",
                                    force_close_trade.id, _liquidity_ledger_audit_exc, exc_info=True,
                                )
                        # AUDIT-01 (post-review correction) — a second,
                        # complementary ADMINISTRATIVE event, recorded
                        # only here because this is the one code path
                        # that actually holds the real staff actor
                        # (request.user). Not a duplicate of the
                        # financial event above: one describes the
                        # money fact, this one describes who did it.
                        from . import broker_audit as _audit
                        _audit.record_admin_event(
                            event_type=_audit.EV_ADMIN_POSITION_FORCE_CLOSE,
                            description=f"Staff {request.user.username} force-closed position on {pos.symbol}",
                            actor_id=request.user.id,
                            account=locked_account or account,
                            trade=force_close_trade,
                            symbol=pos.symbol,
                            request=request,
                            metadata={
                                "reason": "admin_force_close",
                                "pnl": float(pnl),
                                "exit_price": exit_px,
                            },
                        )
                        pos.delete()
                        total_closed += 1
                        total_pnl += pnl

                    if locked_account:
                        locked_account.balance = Decimal(str(running_balance))
                        locked_account.equity  = Decimal(str(running_balance))
                        locked_account.save(update_fields=["balance", "equity"])
                msg = (f"Cerradas {total_closed} posición(es). PnL total: ${total_pnl:+.2f}"
                       if total_closed else "No hay posiciones que coincidan.")
                (messages.success if total_closed else messages.info)(request, msg)
                return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

        # ── Build context ──
        open_positions = list(Position.objects.filter(account=account).order_by("-opened_at"))

        agg = (
            Position.objects.filter(account=account)
            .values("symbol")
            .annotate(
                long_qty=Sum("qty", filter=Q(side="BUY")),
                short_qty=Sum("qty", filter=Q(side="SELL")),
            )
            .order_by("symbol")
        )
        exposure = [
            {"symbol": r["symbol"],
             "long_qty":  float(r["long_qty"]  or 0),
             "short_qty": float(r["short_qty"] or 0),
             "net_qty":   float(r["long_qty"]  or 0) - float(r["short_qty"] or 0)}
            for r in agg
        ]

        try:
            risk_rule = account.risk_rule
        except Exception:
            risk_rule = None

        _violations_qs = TradingViolation.objects.filter(account=account).order_by("-created_at")[:15]
        violations = []
        for _v in _violations_qs:
            _v.excess = round(float(_v.value_at_violation) - float(_v.limit_value), 4)
            violations.append(_v)

        try:
            trader_score = account.trader_score
        except Exception:
            trader_score = None

        dd_snapshots = DrawdownSnapshot.objects.filter(account=account).order_by("-date")[:10]

        peak = float(account.peak_balance or account.balance or 1)
        balance = float(account.balance or 0)
        total_dd_pct = max(0.0, (peak - balance) / peak * 100) if peak > 0 else 0.0

        # Today's realized PnL from ledger
        from django.utils import timezone
        today = timezone.now().date()
        today_pnl = float(
            LedgerEntry.objects.filter(
                account=account,
                event_type=LedgerEntry.EV_REALIZED,
                created_at__date=today,
            ).aggregate(t=Sum("amount"))["t"] or 0
        )
        daily_dd_pct = abs(today_pnl) / peak * 100 if (peak > 0 and today_pnl < 0) else 0.0

        # Challenge progress
        initial_balance = float(
            account.initial_balance
            or _TIER_INITIAL.get(account.tier, 10000)
        )
        profit_gained = balance - initial_balance
        profit_target = float(account.profit_target or 1)
        profit_pct = max(0.0, min(100.0, profit_gained / profit_target * 100)) if profit_target else 0.0

        # Limits from risk rule or tier defaults
        daily_limit  = float(risk_rule.max_daily_loss_pct) if risk_rule else {"10K": 5, "50K": 4, "100K": 3}.get(account.tier, 5)
        max_dd_limit = float(risk_rule.max_drawdown_pct)   if risk_rule else {"10K": 10, "50K": 8, "100K": 6}.get(account.tier, 10)

        upnl = round(float(account.equity or 0) - float(account.balance or 0), 2)

        # Retail margin engine metrics
        retail_margin = None
        if account.account_type == "RETAIL":
            from .risk_engine import compute_margin_state, _MARGIN_THRESHOLDS
            lev = max(1, account.leverage or 50)
            total_mg = sum(float(p.avg_price) * float(p.qty) / lev for p in open_positions)
            eq_f = float(account.equity or account.balance or 0)
            _mg = compute_margin_state(eq_f, total_mg)
            used_pct = _mg["used_margin_pct"]
            if used_pct >= _MARGIN_THRESHOLDS["DANGER"]:
                _mg["status_label"], _mg["status_color"] = "DANGER", "#e74c3c"
            elif used_pct >= _MARGIN_THRESHOLDS["HIGH"]:
                _mg["status_label"], _mg["status_color"] = "HIGH RISK", "#e67e22"
            elif used_pct >= _MARGIN_THRESHOLDS["WARNING"]:
                _mg["status_label"], _mg["status_color"] = "WARNING", "#f1c40f"
            else:
                _mg["status_label"], _mg["status_color"] = "NORMAL", "#27ae60"
            _mg["margin_level_color"] = (
                "#e74c3c" if (_mg["margin_level"] > 0 and _mg["margin_level"] < 100)
                else "#e67e22" if (_mg["margin_level"] > 0 and _mg["margin_level"] < 150)
                else "#27ae60"
            )
            retail_margin = _mg

        context = dict(
            self.admin_site.each_context(request),
            title=f"Dealing Desk — {account.user} / #{account.id} / {account.tier or account.account_type}",
            account=account,
            upnl=upnl,
            open_positions=open_positions,
            exposure=exposure,
            risk_rule=risk_rule,
            violations=violations,
            trader_score=trader_score,
            dd_snapshots=dd_snapshots,
            total_dd_pct=round(total_dd_pct, 2),
            daily_dd_pct=round(daily_dd_pct, 2),
            daily_limit=round(daily_limit, 2),
            max_dd_limit=round(max_dd_limit, 2),
            profit_pct=round(profit_pct, 1),
            profit_gained=round(profit_gained, 2),
            profit_target=round(profit_target, 2),
            today_pnl=round(today_pnl, 2),
            retail_margin=retail_margin,
        )
        return render(request, "admin/dealing_desk_inline.html", context)


# ─────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────

@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "symbol", "side", "qty", "avg_price", "sl", "tp", "external_id", "opened_at")
    list_filter = ("side", "symbol", "opened_at", "account")
    search_fields = ("symbol", "account__user__username", "account__user__email", "external_id")
    readonly_fields = ("opened_at",)
    list_editable = ("sl", "tp")

    # O.6c-1o — MULTIPANEL-01 fix, writer #10 (Django Admin) from the
    # O.6c-1n writer map: Admin was the only Position writer with zero
    # WS propagation, not even a stub. Both changeform_view and
    # changelist_view (the list_editable sl/tp inline-save path) wrap
    # this call in their own transaction.atomic() (Django's own
    # ModelAdmin — verified, not assumed), so transaction.on_commit()
    # here is required, not just idiomatic: publishing directly would
    # fire even if a later step in that same admin transaction rolled
    # back the save.
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from django.db import transaction
        from . import ws_events
        account_id, position_id, symbol = obj.account_id, obj.pk, obj.symbol
        transaction.on_commit(lambda: ws_events.publish_position_changed(
            account_id, action=ws_events.ACTION_UPDATE,
            position_id=position_id, symbol=symbol,
        ))
        # O.6c-1v — OPEN POSITION FEED COVERAGE, writer #8 (Admin
        # create/edit). mark_position_symbol() is sync/thread-safe — safe
        # to call directly from this on_commit callback.
        from market_data.feeds import get_feed_manager
        transaction.on_commit(lambda: get_feed_manager().mark_position_symbol(symbol))

    # Same transaction.atomic() rationale as save_model above. Per O.6c-1o's
    # explicit instruction: account_id/position_id/symbol are captured
    # BEFORE delete_model() removes the row — obj.pk/obj.account_id are
    # None afterward, and the lambda would otherwise close over the
    # post-delete (empty) values.
    def delete_model(self, request, obj):
        from django.db import transaction
        from . import ws_events
        account_id, position_id, symbol = obj.account_id, obj.pk, obj.symbol
        super().delete_model(request, obj)
        transaction.on_commit(lambda: ws_events.publish_position_changed(
            account_id, action=ws_events.ACTION_CLOSE,
            position_id=position_id, symbol=symbol,
        ))
        # O.6c-1v — OPEN POSITION FEED COVERAGE, writer #8 (Admin delete).
        # sync_position_symbol_from_db() re-derives from DB — correctly
        # keeps the feed alive if another Position on the same symbol
        # still exists.
        from market_data.feeds import get_feed_manager
        transaction.on_commit(lambda: get_feed_manager().sync_position_symbol_from_db(symbol))


# ─────────────────────────────────────────────
# Trade
# ─────────────────────────────────────────────

@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    @admin.display(description="P&L")
    def pnl_colored(self, obj):
        v = float(obj.profit_loss or 0)
        color = "#27ae60" if v > 0 else "#e74c3c" if v < 0 else "#888"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:+.2f}")

    list_display = (
        "id", "account", "symbol", "trade_type", "lot_size",
        "entry_price", "exit_price", "pnl_colored",
        "opened_at", "closed_at",
    )
    list_filter = ("trade_type", "symbol", "opened_at", "closed_at", "account")
    search_fields = ("account__user__username", "account__user__email", "symbol")
    readonly_fields = ("opened_at", "closed_at")


# ─────────────────────────────────────────────
# LedgerEntry
# ─────────────────────────────────────────────

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    @admin.display(description="Amount")
    def amount_colored(self, obj):
        v = float(obj.amount or 0)
        color = "#27ae60" if v > 0 else "#e74c3c" if v < 0 else "#888"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:+.2f}")

    list_display  = ("id", "account", "event_type", "amount_colored", "balance_after", "created_at")
    list_filter   = ("event_type", "created_at")
    search_fields = ("account__user__username", "account__user__email", "event_type")
    ordering      = ("-id",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "balance_after")

    _ALL_READONLY = ("account", "event_type", "amount", "balance_after", "meta", "created_at")

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_readonly_fields(self, request, obj=None):
        if not request.user.is_superuser:
            return self._ALL_READONLY
        return self.readonly_fields


# ─────────────────────────────────────────────
# RiskRule
# ─────────────────────────────────────────────

@admin.register(RiskRule)
class RiskRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "account_link", "max_daily_loss_pct", "max_drawdown_pct",
        "max_lot_size", "max_open_positions", "max_exposure_usd",
        "updated_at",
    )
    list_filter = ("account__account_type", "account__tier")
    search_fields = ("account__user__username", "account__user__email")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Account", ordering="account")
    def account_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_change", args=[obj.account_id])
        return format_html('<a href="{}">{}</a>', url, str(obj.account))

    fieldsets = (
        ("Account", {"fields": ("account",)}),
        ("Daily / Drawdown Limits", {"fields": ("max_daily_loss_pct", "max_drawdown_pct")}),
        ("Position Limits", {"fields": ("max_lot_size", "max_open_positions", "max_exposure_usd")}),
        ("Consistency", {"fields": ("consistency_min_trades",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


# ─────────────────────────────────────────────
# TradingViolation
# ─────────────────────────────────────────────

@admin.register(TradingViolation)
class TradingViolationAdmin(admin.ModelAdmin):

    @admin.display(description="Type")
    def type_badge(self, obj):
        color = _VIOLATION_COLORS.get(obj.violation_type, "#888")
        return format_html(
            '<span style="color:{};font-weight:700;font-size:11px">{}</span>',
            color, obj.violation_type,
        )

    @admin.display(description="Breach")
    def breach_display(self, obj):
        v = float(obj.value_at_violation)
        lim = float(obj.limit_value)
        over = v - lim
        return format_html(
            '<span style="color:#e74c3c">{}</span> / {} '
            '<span style="color:#e67e22;font-size:10px">(+{})</span>',
            f"{v:.4f}", f"{lim:.4f}", f"{over:.4f}",
        )

    @admin.display(description="Trader")
    def trader_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_change", args=[obj.account_id])
        user = getattr(obj.account, "user", None)
        label = getattr(user, "username", f"#{obj.account_id}")
        return format_html('<a href="{}">{}</a>', url, label)

    list_display = (
        "id", "trader_link", "type_badge", "breach_display",
        "created_at",
    )
    list_filter = ("violation_type", "created_at", "account__tier")
    search_fields = ("account__user__username", "account__user__email", "violation_type")
    readonly_fields = ("account", "violation_type", "value_at_violation", "limit_value", "meta", "created_at")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


# ─────────────────────────────────────────────
# DrawdownSnapshot
# ─────────────────────────────────────────────

@admin.register(DrawdownSnapshot)
class DrawdownSnapshotAdmin(admin.ModelAdmin):

    @admin.display(description="Daily P&L")
    def daily_pnl_colored(self, obj):
        v = float(obj.daily_pnl or 0)
        color = "#27ae60" if v > 0 else "#e74c3c" if v < 0 else "#888"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:+.2f}")

    @admin.display(description="DD from Peak %")
    def dd_pct_colored(self, obj):
        v = float(obj.drawdown_from_peak or 0)
        color = "#e74c3c" if v >= 7 else "#e67e22" if v >= 3 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.2f}%")

    @admin.display(description="Trader")
    def trader_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_change", args=[obj.account_id])
        user = getattr(obj.account, "user", None)
        label = getattr(user, "username", f"#{obj.account_id}")
        return format_html('<a href="{}">{}</a>', url, label)

    list_display = (
        "id", "trader_link", "date",
        "balance_start", "balance_end",
        "daily_pnl_colored", "daily_pnl_pct",
        "peak_balance", "dd_pct_colored",
    )
    list_filter = ("date", "account__tier")
    search_fields = ("account__user__username", "account__user__email")
    readonly_fields = (
        "account", "date", "balance_start", "balance_end",
        "daily_pnl", "daily_pnl_pct", "peak_balance", "drawdown_from_peak",
    )
    ordering = ("-date", "-id")
    date_hierarchy = "date"


# ─────────────────────────────────────────────
# TraderScore
# ─────────────────────────────────────────────

@admin.register(TraderScore)
class TraderScoreAdmin(admin.ModelAdmin):

    @admin.display(description="Classification")
    def class_badge(self, obj):
        bg, fg = _CLASS_COLORS.get(obj.trader_class, ("#1a1a2a", "#aaa"))
        icon = {
            "ELITE": "★", "CONSISTENT": "✓", "NORMAL": "·",
            "GAMBLER": "🎰", "MARTINGALE": "↑↑", "RISKY": "⚠",
            "SCALPER": "⚡", "TOXIC": "☠",
        }.get(obj.trader_class, "·")
        return format_html(
            '<span style="background:{};color:{};padding:4px 14px;border-radius:20px;'
            'font-size:12px;font-weight:800;letter-spacing:.06em;white-space:nowrap">{} {}</span>',
            bg, fg, icon, obj.trader_class,
        )

    @admin.display(description="Danger")
    def danger_indicator(self, obj):
        tox = float(obj.toxicity_score or 0)
        gam = float(obj.gambler_score  or 0)
        mart = float(obj.martingale_rate or 0) * 100
        danger = max(tox, gam, mart)
        if danger >= 70:
            color, label = "#ef5350", "HIGH"
        elif danger >= 40:
            color, label = "#e67e22", "MED"
        else:
            color, label = "#27ae60", "LOW"
        bar_w = min(int(danger), 100)
        return format_html(
            '<div style="display:flex;align-items:center;gap:6px;">'
            '<div style="background:rgba(255,255,255,.06);border-radius:3px;width:60px;height:6px;overflow:hidden;">'
            '<div style="width:{}%;height:6px;background:{};border-radius:3px;"></div></div>'
            '<span style="color:{};font-size:11px;font-weight:700;">{}</span>'
            '</div>',
            bar_w, color, color, label,
        )

    @admin.display(description="Win Rate")
    def win_rate_display(self, obj):
        v = float(obj.win_rate or 0)
        color = "#27ae60" if v >= 55 else "#e67e22" if v >= 40 else "#e74c3c"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}%")

    @admin.display(description="Profit Factor")
    def pf_display(self, obj):
        v = float(obj.profit_factor or 0)
        color = "#27ae60" if v >= 1.5 else "#e67e22" if v >= 1.0 else "#e74c3c"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.2f}")

    @admin.display(description="Consistency")
    def consistency_display(self, obj):
        v = float(obj.consistency_score or 0)
        color = "#27ae60" if v >= 60 else "#e67e22" if v >= 40 else "#e74c3c"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}")

    @admin.display(description="Martingale %")
    def martingale_display(self, obj):
        v = float(obj.martingale_rate or 0) * 100
        color = "#e74c3c" if v >= 25 else "#e67e22" if v >= 10 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}%")

    @admin.display(description="Routing")
    def routing_badge(self, obj):
        colors = {
            "ELITE":           ("#0a2a1a", "#00e676"),
            "INTERNAL":        ("#1a1a2a", "#7986cb"),
            "REVIEW":          ("#2a1a00", "#ffa726"),
            "HEDGE_CANDIDATE": ("#2a0000", "#ef5350"),
        }
        bg, fg = colors.get(obj.routing_profile, ("#1a1a1a", "#aaa"))
        return _badge(obj.routing_profile, bg, fg)

    @admin.display(description="Hold Avg")
    def hold_time_display(self, obj):
        secs = float(obj.avg_hold_time_seconds or 0)
        if secs >= 3600:
            return f"{secs/3600:.1f}h"
        if secs >= 60:
            return f"{secs/60:.1f}m"
        return f"{secs:.0f}s"

    @admin.display(description="Toxicity")
    def toxicity_display(self, obj):
        v = float(obj.toxicity_score or 0)
        color = "#e74c3c" if v >= 70 else "#e67e22" if v >= 40 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}")

    @admin.display(description="Gambler")
    def gambler_display(self, obj):
        v = float(obj.gambler_score or 0)
        color = "#f1c40f" if v >= 60 else "#e67e22" if v >= 30 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}")

    @admin.display(description="Freq/day")
    def freq_display(self, obj):
        v = float(obj.trade_frequency or 0)
        color = "#e74c3c" if v >= 20 else "#e67e22" if v >= 10 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>', color, f"{v:.1f}")

    @admin.display(description="Trader")
    def trader_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_change", args=[obj.account_id])
        user = getattr(obj.account, "user", None)
        label = getattr(user, "username", f"#{obj.account_id}")
        return format_html('<a href="{}">{}</a>', url, label)

    list_display = (
        "id", "trader_link",
        "class_badge", "routing_badge", "danger_indicator",
        "toxicity_display", "gambler_display",
        "win_rate_display", "pf_display", "consistency_display",
        "martingale_display", "hold_time_display", "freq_display",
        "last_evaluated",
    )
    list_filter = ("trader_class", "routing_profile", "last_evaluated")
    search_fields = ("account__user__username", "account__user__email")
    readonly_fields = (
        "account", "trader_class", "routing_profile",
        "win_rate", "profit_factor", "avg_lot_size", "consistency_score",
        "avg_rr", "pnl_volatility",
        "martingale_rate", "lot_growth_rate", "scalping_ratio",
        "avg_hold_time_seconds", "toxicity_score", "gambler_score",
        "trade_frequency", "max_consecutive_losses", "max_consecutive_wins",
        "last_evaluated",
    )
    fieldsets = (
        ("Classification", {"fields": ("account", "trader_class", "routing_profile", "last_evaluated")}),
        ("Performance", {"fields": ("win_rate", "profit_factor", "avg_lot_size", "consistency_score", "avg_rr", "pnl_volatility")}),
        ("Behavioral Signals", {"fields": (
            "martingale_rate", "lot_growth_rate", "scalping_ratio",
            "avg_hold_time_seconds", "toxicity_score", "gambler_score",
            "trade_frequency", "max_consecutive_losses", "max_consecutive_wins",
        )}),
    )
    ordering = ("-last_evaluated",)

    actions = [recalc_trader_scores]


# ─────────────────────────────────────────────
# Purchase / Deposit
# ─────────────────────────────────────────────

@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tier", "code", "used", "created_at")
    list_filter = ("tier", "used", "created_at")
    search_fields = ("user__username", "user__email", "code")
    readonly_fields = ("created_at",)


@admin.register(Deposit)
class DepositAdmin(admin.ModelAdmin):
    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "finished":  ("#0a2a1a", "#27ae60"),
            "confirmed": ("#0a2218", "#26a69a"),
            "failed":    ("#2a0000", "#e74c3c"),
            "expired":   ("#1a1a1a", "#888"),
            "pending":   ("#1a1a2a", "#7986cb"),
            "waiting":   ("#1a1a2a", "#3498db"),
        }
        bg, fg = colors.get(obj.status, ("#1a1a1a", "#aaa"))
        return _badge(obj.get_status_display(), bg, fg)

    @admin.display(description="Challenge?", boolean=True)
    def is_challenge(self, obj):
        return obj.challenge_product_id is not None

    list_display  = ("id", "user", "amount_usd", "crypto_currency", "status_badge", "is_challenge", "credited", "created_at", "confirmed_at")
    list_filter   = ("status", "crypto_currency", "credited", "created_at")
    search_fields = ("user__username", "user__email", "nowpayments_payment_id")
    ordering      = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "confirmed_at", "nowpayments_payment_id", "nowpayments_invoice_url")


# ─────────────────────────────────────────────
# Exposure / Dealer Analytics
# ─────────────────────────────────────────────

@admin.action(description="📸 Guardar Snapshot de Exposición ahora")
def take_exposure_snapshot(modeladmin, request, queryset):
    from .exposure_engine import save_snapshot
    snap = save_snapshot()
    modeladmin.message_user(
        request,
        f"Snapshot #{snap.pk} guardado — net=${float(snap.net_exposure_usd):,.2f}, "
        f"{snap.total_open_positions} posiciones.",
    )


class SymbolExposureInline(admin.TabularInline):
    model = SymbolExposure
    extra = 0
    can_delete = False
    readonly_fields = (
        "symbol", "long_usd", "short_usd", "net_usd",
        "trader_count", "concentration_pct", "unrealized_pnl", "is_high_risk",
    )
    fields = readonly_fields
    ordering = ("-concentration_pct",)

    def has_add_permission(self, request, obj=None):
        return False


class TraderClassExposureInline(admin.TabularInline):
    model = TraderClassExposure
    extra = 0
    can_delete = False
    readonly_fields = (
        "trader_class", "routing_profile", "account_count",
        "long_usd", "short_usd", "net_usd", "unrealized_pnl",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BrokerSnapshot)
class BrokerSnapshotAdmin(admin.ModelAdmin):

    # ── display helpers ──

    @admin.display(description="Net Exposure")
    def net_col(self, obj):
        v = float(obj.net_exposure_usd)
        color = "#ef5350" if abs(v) > 10000 else "#e67e22" if abs(v) > 5000 else "#27ae60"
        return format_html('<span style="color:{};font-weight:700">{}</span>',
                           color, f"${v:+,.2f}")

    @admin.display(description="UPnL")
    def upnl_col(self, obj):
        v = float(obj.total_unrealized_pnl)
        color = "#27ae60" if v >= 0 else "#ef5350"
        return format_html('<span style="color:{};font-weight:700">{}</span>',
                           color, f"${v:+,.2f}")

    @admin.display(description="Broker PnL (sim)")
    def broker_pnl_col(self, obj):
        v = float(obj.broker_pnl_unrealized)
        color = "#27ae60" if v >= 0 else "#ef5350"
        return format_html('<span style="color:{};font-weight:700">{}</span>',
                           color, f"${v:+,.2f}")

    @admin.display(description="Flags")
    def flags_col(self, obj):
        n = len(obj.risk_flags or [])
        if n == 0:
            return format_html('<span style="color:#27ae60">✓ 0</span>')
        color = "#ef5350" if any(f.get("severity") == "HIGH" for f in obj.risk_flags) else "#e67e22"
        return format_html('<span style="color:{};font-weight:700">⚠ {}</span>', color, n)

    @admin.display(description="Live Analytics")
    def live_link(self, obj):
        url = reverse("admin:broker_live_analytics")
        return format_html('<a href="{}">→ Live Desk</a>', url)

    list_display = (
        "id", "created_at",
        "total_accounts", "total_open_positions",
        "net_col", "upnl_col", "broker_pnl_col",
        "internal_exposure_usd", "hedge_candidate_usd",
        "flags_col", "live_link",
    )
    list_filter  = ("created_at",)
    readonly_fields = (
        "created_at", "total_accounts", "total_open_positions",
        "total_long_usd", "total_short_usd", "net_exposure_usd",
        "total_unrealized_pnl", "total_realized_pnl_today",
        "internal_exposure_usd", "review_exposure_usd", "hedge_candidate_usd",
        "broker_pnl_unrealized", "broker_pnl_today", "risk_flags",
    )
    inlines  = [SymbolExposureInline, TraderClassExposureInline]
    actions  = [take_exposure_snapshot]
    ordering = ("-created_at",)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["live_url"]     = reverse("admin:broker_live_analytics")
        extra_context["snapshot_url"] = reverse("admin:broker_take_snapshot")
        return super().changelist_view(request, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "live/",
                self.admin_site.admin_view(self.live_analytics_view),
                name="broker_live_analytics",
            ),
            path(
                "snapshot/",
                self.admin_site.admin_view(self.take_snapshot_view),
                name="broker_take_snapshot",
            ),
            path(
                "shadow-exposure/",
                self.admin_site.admin_view(self.shadow_exposure_view),
                name="broker_shadow_exposure",
            ),
        ]
        return custom + urls

    def live_analytics_view(self, request):
        from .exposure_engine import compute_live_analytics
        data = compute_live_analytics()

        # Build risk-flag severity counts
        high_flags   = [f for f in data["risk_flags"] if f.get("severity") == "HIGH"]
        medium_flags = [f for f in data["risk_flags"] if f.get("severity") == "MEDIUM"]

        # Long/Short ratio
        total_gross = data["total_long_usd"] + data["total_short_usd"]
        long_pct    = round(data["total_long_usd"]  / total_gross * 100, 1) if total_gross else 50.0
        short_pct   = round(data["total_short_usd"] / total_gross * 100, 1) if total_gross else 50.0

        context = dict(
            self.admin_site.each_context(request),
            title      = "Broker Exposure Desk — Live Analytics",
            data       = data,
            high_flags = high_flags,
            medium_flags = medium_flags,
            long_pct   = long_pct,
            short_pct  = short_pct,
            snapshot_url = reverse("admin:broker_take_snapshot"),
        )
        return render(request, "admin/broker_analytics.html", context)

    def take_snapshot_view(self, request):
        from .exposure_engine import save_snapshot
        snap = save_snapshot()
        messages.success(
            request,
            f"Snapshot #{snap.pk} guardado — net=${float(snap.net_exposure_usd):,.2f}, "
            f"{snap.total_open_positions} posiciones.",
        )
        return redirect("admin:broker_live_analytics")

    def shadow_exposure_view(self, request):
        """
        BOOK-06e — read-only observability surface for
        calculate_shadow_broker_exposure() (BOOK-06d). Restricted to
        is_superuser (approved 2026-07-27) — stricter than
        live_analytics_view's plain is_staff, because this screen still
        exposes an experimental calculation used only for architectural/
        business validation while the Dealing Desk stays in Shadow Mode.
        May be relaxed to is_staff once the calculation exits Shadow Mode
        — that is a future, separately-authorized decision, not made here.

        Computed strictly on demand, on every GET — no cache, no Celery,
        same discipline as live_analytics_view. Never writes anything:
        calculate_shadow_broker_exposure() itself is read-only by
        contract (BOOK-06d), and this view adds no write of its own.

        The outer try/except is a second, defensive layer — the same
        "belt and suspenders" already used throughout BOOK-04/05/06's own
        call sites — on top of calculate_shadow_broker_exposure()'s own
        internal fail-open contract (which already never raises): a
        failure anywhere in this view (bad filter value, unexpected
        error) must never produce a 500, only a visible warning with an
        all-zero comparison.
        """
        from django.core.exceptions import PermissionDenied

        if not request.user.is_superuser:
            raise PermissionDenied("Esta vista requiere permisos de superusuario.")

        from .broker_risk_shadow import ShadowExposureComparison, calculate_shadow_broker_exposure

        filters = {}
        calc_failed = False

        try:
            symbol = (request.GET.get("symbol") or "").strip()
            if symbol:
                filters["symbol"] = symbol

            account_id_raw = (request.GET.get("account_id") or "").strip()
            if account_id_raw:
                try:
                    filters["account_id"] = int(account_id_raw)
                except ValueError:
                    messages.warning(request, f"account_id inválido ignorado: {account_id_raw!r}")

            account_type = (request.GET.get("account_type") or "").strip()
            if account_type:
                filters["account_type"] = account_type

            status = (request.GET.get("status") or "").strip()
            if status:
                filters["status"] = status

            trader_class = (request.GET.get("trader_class") or "").strip()
            if trader_class:
                filters["trader_class"] = trader_class

            comparison = calculate_shadow_broker_exposure(**filters)
        except Exception as exc:
            import logging
            logging.getLogger("simulator.admin").error(
                "[admin] shadow exposure view failed: %r", exc, exc_info=True,
            )
            comparison = ShadowExposureComparison()
            calc_failed = True

        # BOOK-06h.3 — read-only canary status indicator (closes RC-1
        # Finding F-05's UI half). Plain settings reads, same pattern
        # already used by broker_risk.py's own gate — no write, no
        # form, no action, nothing here can change the configuration.
        # Deliberately distinct from `comparison` above: this reflects
        # the REAL canary configuration consumed by
        # broker_risk.py::validate_new_order(); the shadow numbers
        # above are a separate, always-global preview (never scoped to
        # this allowlist) — see broker_risk_shadow.py's own docstring.
        from django.conf import settings as _dj_settings
        canary_enabled = bool(getattr(_dj_settings, "DEALING_DESK_EXPOSURE_ENABLED", False))
        canary_account_count = len(getattr(_dj_settings, "DEALING_DESK_EXPOSURE_ACCOUNT_IDS", frozenset()))

        context = dict(
            self.admin_site.each_context(request),
            title="Shadow Exposure Observability (BOOK-06e)",
            comparison=comparison,
            calc_failed=calc_failed,
            applied_filters=filters,
            live_url=reverse("admin:broker_live_analytics"),
            canary_enabled=canary_enabled,
            canary_account_count=canary_account_count,
        )
        return render(request, "admin/shadow_exposure_observability.html", context)


# ─────────────────────────────────────────────
# WithdrawalRequest
# ─────────────────────────────────────────────

import logging as _logging
_wlog = _logging.getLogger(__name__)


def _mask_wallet(addr: str) -> str:
    if not addr or len(addr) <= 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


@admin.action(description="✅ Aprobar — enviar pago crypto vía NowPayments")
def approve_withdrawals(modeladmin, request, queryset):
    from . import nowpayments as _np
    from .wallet_ledger import get_or_create_wallet
    from .models import WalletTransaction
    from django.core.mail import send_mail
    from django.conf import settings as _cfg
    from django.urls import reverse as _rev
    from django.db import transaction as _tx

    pending = queryset.filter(status=WithdrawalRequest.STATUS_PENDING)
    if not pending.exists():
        modeladmin.message_user(request, "No hay retiros pendientes seleccionados.", messages.WARNING)
        return

    ok, errs = 0, []
    for wr in pending:
        try:
            # Atomically claim the WR as APPROVED before making the external API call.
            # Only one concurrent admin session can win this update; the loser gets
            # claimed=0 and skips, preventing duplicate payouts.
            with _tx.atomic():
                claimed = WithdrawalRequest.objects.select_for_update().filter(
                    pk=wr.pk,
                    status=WithdrawalRequest.STATUS_PENDING,
                ).update(
                    status      = WithdrawalRequest.STATUS_APPROVED,
                    reviewed_by = request.user,
                    reviewed_at = now(),
                )
            if not claimed:
                _wlog.warning("[admin] approve wr #%d skipped — status changed concurrently", wr.pk)
                continue

            crypto_amount = _np.estimate_price(wr.amount_usd, wr.crypto_currency)
            cb_url = request.build_absolute_uri(
                reverse("simulator:withdraw_payout_callback")
            )
            data      = _np.create_payout(wr.wallet_address, wr.crypto_currency, crypto_amount, wr.id, cb_url)
            batch_wds = data.get("withdrawals", [])
            batch_id  = str(data.get("id", ""))
            payout_id = str(batch_wds[0].get("id", "")) if batch_wds else ""

            WithdrawalRequest.objects.filter(pk=wr.pk).update(
                status           = WithdrawalRequest.STATUS_PROCESSING,
                np_batch_id      = batch_id,
                np_payout_id     = payout_id,
                np_payout_status = str(data.get("status", "")),
                crypto_amount    = crypto_amount,
            )
            from .audit import log_audit, EV_WITHDRAW_APPROVED
            log_audit(
                request, EV_WITHDRAW_APPROVED,
                f"Withdrawal #{wr.id} approved by {request.user.username} — ${wr.amount_usd}",
                detail={
                    "withdrawal_id": wr.id,
                    "amount_usd": str(wr.amount_usd),
                    "currency": wr.crypto_currency,
                    "np_batch_id": batch_id,
                    "np_payout_id": payout_id,
                    "reviewed_by": request.user.username,
                },
            )
            try:
                from .withdrawal_emails import send_withdrawal_status_email, EVENT_APPROVED
                send_withdrawal_status_email(wr, EVENT_APPROVED)
            except Exception as mail_exc:
                _wlog.warning("[admin] approve email queuing failed wr=%d: %s", wr.id, mail_exc)
            ok += 1

        except Exception as exc:
            _wlog.error("[admin] approve withdrawal #%d failed: %s", wr.id, exc, exc_info=True)
            # Roll back to PENDING so admin can retry
            WithdrawalRequest.objects.filter(
                pk=wr.pk,
                status=WithdrawalRequest.STATUS_APPROVED,
            ).update(status=WithdrawalRequest.STATUS_PENDING)
            errs.append(f"#{wr.id}: {exc}")

    if ok:
        modeladmin.message_user(request, f"{ok} retiro(s) aprobados y enviados.", messages.SUCCESS)
    for e in errs:
        modeladmin.message_user(request, f"Error — {e}", messages.ERROR)


approve_withdrawals = superuser_required_action(approve_withdrawals)


@admin.action(description="❌ Rechazar — devolver fondos al wallet")
def reject_withdrawals(modeladmin, request, queryset):
    from .wallet_ledger import credit_wallet, get_or_create_wallet
    from .models import WalletTransaction
    from django.core.mail import send_mail
    from django.conf import settings as _cfg
    from django.db import transaction as _tx

    pending = queryset.filter(status=WithdrawalRequest.STATUS_PENDING)
    if not pending.exists():
        modeladmin.message_user(request, "No hay retiros pendientes seleccionados.", messages.WARNING)
        return

    count = 0
    for wr in pending:
        try:
            with _tx.atomic():
                # Re-check status under a row lock — prevents double-rejection race.
                wr_locked = WithdrawalRequest.objects.select_for_update().filter(
                    pk=wr.pk,
                    status=WithdrawalRequest.STATUS_PENDING,
                ).first()
                if wr_locked is None:
                    _wlog.warning("[admin] reject wr #%d skipped — status changed concurrently", wr.pk)
                    continue

                wallet, _ = get_or_create_wallet(wr.user)
                credit_wallet(
                    wallet.id,
                    wr.amount_usd,
                    WalletTransaction.TX_CORRECTION,
                    note=f"Refund — retiro #{wr.id} rechazado por admin",
                    initiated_by=request.user,
                )
                WithdrawalRequest.objects.filter(pk=wr.pk).update(
                    status      = WithdrawalRequest.STATUS_REJECTED,
                    reviewed_by = request.user,
                    reviewed_at = now(),
                )

            from .audit import log_audit, EV_WITHDRAW_REJECTED
            log_audit(
                request, EV_WITHDRAW_REJECTED,
                f"Withdrawal #{wr.id} rejected by {request.user.username} — ${wr.amount_usd} refunded",
                detail={
                    "withdrawal_id": wr.id,
                    "amount_usd": str(wr.amount_usd),
                    "currency": wr.crypto_currency,
                    "reviewed_by": request.user.username,
                },
            )
            try:
                from .withdrawal_emails import send_withdrawal_status_email, EVENT_REJECTED
                send_withdrawal_status_email(wr, EVENT_REJECTED)
            except Exception as mail_exc:
                _wlog.warning("[admin] reject email queuing failed wr=%d: %s", wr.id, mail_exc)
            count += 1
        except Exception as exc:
            _wlog.error("[admin] reject withdrawal #%d failed: %s", wr.id, exc, exc_info=True)
            modeladmin.message_user(request, f"Error #{wr.id}: {exc}", messages.ERROR)

    if count:
        modeladmin.message_user(request, f"{count} retiro(s) rechazados y fondos devueltos.", messages.SUCCESS)


reject_withdrawals = superuser_required_action(reject_withdrawals)


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "pending":    ("#1a1a2a", "#7986cb"),
            "approved":   ("#0a2a1a", "#27ae60"),
            "rejected":   ("#2a0000", "#e74c3c"),
            "processing": ("#2a1a00", "#f1c40f"),
            "completed":  ("#0a2a1a", "#00e676"),
            "failed":     ("#2a0000", "#ef5350"),
        }
        bg, fg = colors.get(obj.status, ("#1a1a1a", "#aaa"))
        return _badge(obj.get_status_display(), bg, fg)

    @admin.display(description="User")
    def user_col(self, obj):
        return format_html(
            '<strong>{}</strong><br><small style="color:#888">{}</small>',
            obj.user.username, obj.user.email,
        )

    @admin.display(description="Amount")
    def amount_col(self, obj):
        return format_html('<span style="color:#FFD700;font-weight:700">${}</span>', obj.amount_usd)

    @admin.display(description="Address")
    def address_short(self, obj):
        a = obj.wallet_address
        return f"{a[:10]}…{a[-6:]}" if len(a) > 18 else a

    @admin.display(description="Crypto Amount")
    def crypto_col(self, obj):
        if not obj.crypto_amount:
            return "—"
        return f"{obj.crypto_amount} {obj.crypto_currency.upper()}"

    list_display  = (
        "id", "user_col", "amount_col", "crypto_currency", "address_short",
        "status_badge", "crypto_col", "np_payout_id", "created_at", "reviewed_by",
    )
    list_filter   = ("status", "crypto_currency", "created_at")
    search_fields = ("user__username", "user__email", "wallet_address", "np_payout_id", "np_batch_id")
    ordering      = ("-created_at",)
    date_hierarchy = "created_at"
    actions       = [approve_withdrawals, reject_withdrawals]

    readonly_fields = (
        "user", "amount_usd", "crypto_currency", "wallet_address",
        "debit_tx", "np_payout_id", "np_batch_id", "np_payout_status",
        "crypto_amount", "reviewed_by", "reviewed_at", "created_at", "updated_at",
    )

    fieldsets = (
        ("Request", {
            "fields": ("user", "amount_usd", "crypto_currency", "wallet_address"),
        }),
        ("Review", {
            "fields": ("status", "admin_note", "reviewed_by", "reviewed_at"),
        }),
        ("NowPayments Payout", {
            "fields": ("np_batch_id", "np_payout_id", "np_payout_status", "crypto_amount"),
        }),
        ("Ledger", {
            "fields": ("debit_tx",),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields":  ("created_at", "updated_at"),
        }),
    )


# ─────────────────────────────────────────────
# Treasury Audit & Reconciliation — O.2e-1
#
# Read-only observation surface only. No path here creates, edits or
# deletes a Wallet / WalletTransaction / InternalTransfer, and none of
# it calls credit_wallet() / debit_wallet() / transfer_to_account() /
# transfer_to_wallet() — those remain the exclusive write path
# (wallet_ledger.py, untouched by this block). The one action below
# (Verify Wallet Consistency) calls the already-existing, already-tested
# reconcile_wallet() — itself read-only by contract — and only ever
# reports what it finds via messages.
#
# Treasury Private Operations (moving money, corrections, reversals,
# payouts) is explicitly out of scope and is not started here.
# ─────────────────────────────────────────────

class WalletTransactionInline(admin.TabularInline):
    model = WalletTransaction
    extra = 0
    can_delete = False
    fk_name = "wallet"
    readonly_fields = (
        "tx_type", "amount", "balance_after", "initiated_by", "created_at",
    )
    fields = readonly_fields
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display   = ("user", "currency", "available_balance", "pending_balance", "updated_at")
    list_filter    = ("currency",)
    search_fields  = ("user__username", "user__email")
    readonly_fields = [f.name for f in Wallet._meta.fields]
    inlines = [WalletTransactionInline]
    actions = ["verify_wallet_consistency"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Verify Wallet Consistency")
    def verify_wallet_consistency(self, request, queryset):
        """
        Read-only audit action. Calls reconcile_wallet() (wallet_ledger.py,
        untouched) once per selected wallet and only reports the result —
        never writes anything.
        """
        from .wallet_ledger import reconcile_wallet

        results = [reconcile_wallet(w.pk) for w in queryset]
        clean = [r for r in results if r["ok"]]
        drifted = [r for r in results if not r["ok"]]

        if drifted:
            detail = "; ".join(
                f"wallet #{r['wallet_id']} stored={r['stored']} computed={r['computed']} drift={r['drift']}"
                for r in drifted
            )
            self.message_user(
                request,
                f"{len(clean)} wallet(s) consistent — {len(drifted)} with drift: {detail}",
                level=messages.WARNING,
            )
        else:
            self.message_user(
                request,
                f"Verified {len(clean)} wallet(s) — all consistent (drift = 0).",
                level=messages.SUCCESS,
            )


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display   = ("wallet", "tx_type", "amount", "balance_after", "initiated_by", "created_at")
    list_filter    = ("tx_type", "created_at")
    search_fields  = ("wallet__user__username", "note")
    readonly_fields = [f.name for f in WalletTransaction._meta.fields]
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InternalTransfer)
class InternalTransferAdmin(admin.ModelAdmin):
    list_display   = ("wallet", "trading_account", "direction", "amount", "status", "initiated_by", "created_at")
    list_filter    = ("direction", "status", "created_at")
    search_fields  = ("wallet__user__username", "trading_account__id")
    readonly_fields = [f.name for f in InternalTransfer._meta.fields]
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ─────────────────────────────────────────────
# Treasury Private Operations — O.2g-1 (infrastructure only)
#
# Read-only observation surface only, same discipline as the Treasury
# Audit & Reconciliation block above. No add/change/delete, no action,
# no request/approve/reject/execute path — none of that exists in the
# codebase yet. This registration exists purely so the schema created
# by this block is visible, exactly like Wallet/WalletTransaction were
# registered read-only (O.2e-1) before any action was added.
# ─────────────────────────────────────────────

def _treasury_wallet_confirmation_data():
    """
    O.3a-5 — one row of confirmation-panel data per Wallet, embedded in the
    "New Treasury Request" page as a JSON blob and looked up client-side
    by WalletChoiceField's <select> value on change. Display-only: this
    never writes anything, and the values shown (balances, KYC status) are
    read straight from the DB at page-render time, same discipline as
    every other read-only observability view in this file.

    KYC status is looked up via a single extra query (not one per wallet)
    to avoid N+1 — a user with no KYCProfile row shows "Not Started",
    matching KYCProfile.STATUS_NOT_STARTED's own default semantics even
    though no row exists yet for that user.
    """
    kyc_status_by_user_id = dict(
        KYCProfile.objects.values_list("user_id", "status")
    )
    kyc_labels = dict(KYCProfile.STATUS_CHOICES)

    data = {}
    for wallet in Wallet.objects.select_related("user").order_by("user__username"):
        kyc_code = kyc_status_by_user_id.get(wallet.user_id, KYCProfile.STATUS_NOT_STARTED)
        data[str(wallet.pk)] = {
            "username": wallet.user.username,
            "email": wallet.user.email or "—",
            "wallet": str(wallet),
            "available_balance": f"{wallet.available_balance:,.2f}",
            "pending_balance": f"{wallet.pending_balance:,.2f}",
            "currency": wallet.currency,
            "kyc_status": kyc_labels.get(kyc_code, kyc_code),
        }
    return data


# O.3c-5b — pure display formatting for the read-only recovery banner
# and confirmation screen. Neither function decides eligibility, case,
# or block_reason — those come untouched from inspect_stuck_treasury_
# execution() (treasury_execution_recovery.py, unmodified). This is
# presentation only: turning a case code / raw seconds count into
# operator-facing text, same discipline as _treasury_wallet_
# confirmation_data() above already applies to wallet balances.
_TREASURY_RECOVERY_CASE_LABELS = {
    "CASE_A": "Clean, past age threshold — eligible candidate",
    "CASE_B": "⚠ wallet_transaction already linked — structurally anomalous, never eligible",
    "CASE_C": "Age confirmed but below threshold — possibly still in flight",
    "CASE_D": "Age unknown — no EXECUTION_STARTED event found",
    "CASE_E": "⚠ EXECUTED/FAILED audit event already exists — audit inconsistency",
    "CASE_F": "executed_by missing or inactive (informational, does not block eligibility by itself)",
}


def _treasury_recovery_case_label(case):
    return _TREASURY_RECOVERY_CASE_LABELS.get(case, case)


def _treasury_recovery_age_display(age_seconds):
    if age_seconds is None:
        return "unknown"
    total = int(age_seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# O.3d-4 — Treasury Operational Dashboard. Read-only by construction:
# "current state" comes straight from inspect_stuck_treasury_execution()
# (O.3c-4b, unmodified — no eligibility/case logic is re-derived here),
# "history" comes straight from BrokerAuditEvent rows already persisted
# by observe_stuck_treasury_executions() (O.3d-2/3, unmodified). This
# function never calls mark_treasury_execution_failed(), never imports
# wallet_ledger.py, and never assigns to any TreasuryOperationRequest,
# Wallet or WalletTransaction field — it only reads and serializes.
#
# Deliberately does NOT bake per-viewer authorization (the Recovery
# link) into this payload — this dict is what gets cached in Redis and
# shared across every viewer for up to 30s (same pattern as Control
# Center's _compute_control_data()), and "can THIS user recover THIS
# row" depends on request.user (self-conflict with requested_by/
# approved_by), which must never be memoized across different viewers.
# treasury_operational_dashboard_data() below adds recover_url per
# request, AFTER the cache lookup, from the cheap requested_by_id/
# approved_by_id/eligible fields already present in each candidate dict.
TREASURY_DASHBOARD_HISTORY_LIMIT = 25  # same magnitude as Control Center's recent_events(25)

_TREASURY_CASE_ORDER = ("CASE_A", "CASE_B", "CASE_C", "CASE_D", "CASE_E", "CASE_F")


def _compute_treasury_operational_dashboard_data():
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from . import broker_audit as _broker_audit_module
    from .models import BrokerAuditEvent
    from .treasury_execution_recovery import inspect_stuck_treasury_execution

    User = get_user_model()

    candidates = inspect_stuck_treasury_execution()

    case_counts = {case: 0 for case in _TREASURY_CASE_ORDER}
    total_eligible = 0
    candidate_rows = []
    for c in candidates:
        case_counts[c.case] = case_counts.get(c.case, 0) + 1
        if c.eligible:
            total_eligible += 1

        instance = c.instance
        candidate_rows.append({
            "treasury_operation_request_id": instance.pk,
            "operation_type": instance.operation_type,
            "operation_type_display": instance.get_operation_type_display(),
            "amount": str(instance.amount),
            "wallet_id": instance.wallet_id,
            "wallet_display": str(instance.wallet),
            "wallet_user_username": instance.wallet.user.username,
            "requested_by_id": instance.requested_by_id,
            "approved_by_id": instance.approved_by_id,
            "executed_by_id": instance.executed_by_id,
            "executed_by_username": instance.executed_by.username if instance.executed_by_id else None,
            "executed_by_is_active": c.executed_by_is_active,
            "age_seconds": c.age_seconds,
            "age_display": _treasury_recovery_age_display(c.age_seconds),
            "age_confidence": c.age_confidence,
            "case": c.case,
            "case_label": _treasury_recovery_case_label(c.case),
            "eligible": c.eligible,
            "block_reason": c.block_reason,
            "has_wallet_transaction": c.has_wallet_transaction,
            "has_started_event": c.has_started_event,
            "has_executed_event": c.has_executed_event,
            "has_failed_event": c.has_failed_event,
            "detail_url": reverse(
                "admin:simulator_treasuryoperationrequest_change", args=[instance.pk],
            ),
        })

    history_events = list(
        BrokerAuditEvent.objects
        .filter(event_type=_broker_audit_module.EV_TREASURY_STUCK_EXECUTION_OBSERVED)
        .order_by("-timestamp", "-id")[:TREASURY_DASHBOARD_HISTORY_LIMIT]
    )
    executed_by_ids = {
        e.metadata.get("executed_by_id")
        for e in history_events
        if e.metadata.get("executed_by_id") is not None
    }
    executed_by_usernames = dict(
        User.objects.filter(pk__in=executed_by_ids).values_list("pk", "username")
    )

    history_rows = [
        {
            "timestamp": e.timestamp.isoformat(),
            "treasury_operation_request_id": e.metadata.get("treasury_operation_request_id"),
            "case": e.metadata.get("case"),
            "case_label": _treasury_recovery_case_label(e.metadata.get("case")),
            "severity": e.severity,
            "eligible": e.metadata.get("eligible"),
            "age_seconds": e.metadata.get("age_seconds"),
            "age_display": _treasury_recovery_age_display(e.metadata.get("age_seconds")),
            "block_reason": e.metadata.get("block_reason"),
            "executed_by_id": e.metadata.get("executed_by_id"),
            "executed_by_username": executed_by_usernames.get(e.metadata.get("executed_by_id")),
            "metadata": e.metadata,
        }
        for e in history_events
    ]

    return {
        "ts": timezone.now().isoformat(),
        "summary": {
            "total_executing": len(candidate_rows),
            "total_eligible": total_eligible,
            "case_counts": case_counts,
        },
        "candidates": candidate_rows,
        "history": history_rows,
        "history_limit": TREASURY_DASHBOARD_HISTORY_LIMIT,
    }


@admin.register(TreasuryOperationRequest)
class TreasuryOperationRequestAdmin(admin.ModelAdmin):
    list_display   = (
        "id", "operation_type", "wallet", "amount", "status",
        "requested_by", "requested_at",
    )
    list_filter    = ("operation_type", "status", "category", "requested_at")
    search_fields  = ("wallet__user__username", "reference", "reason")
    # O.5e-1 — "evidence" is swapped for the evidence_display() method
    # below so this readonly field renders our secure-media link instead
    # of Django's default readonly FileField rendering (display_for_field,
    # django/contrib/admin/utils.py), which builds `<a href="{value.url}">`
    # straight from FileSystemStorage.url() — unauthenticated/unauthorized
    # under any config that actually serves MEDIA_URL.
    readonly_fields = [
        "evidence_display" if f.name == "evidence" else f.name
        for f in TreasuryOperationRequest._meta.fields
    ]
    # Without this, Django's own get_fields()/get_fieldsets() default
    # machinery re-adds the real "evidence" field alongside
    # "evidence_display" above (it's no longer in readonly_fields, so
    # Django treats it as a candidate real form field again) — and, since
    # has_change_permission() is hard-False, still renders it read-only
    # via its own default display_for_field(), i.e. the exact raw
    # `<a href="{MEDIA_URL}...">` link this change was meant to remove.
    exclude = ("evidence",)
    ordering       = ("-requested_at", "-id")

    @admin.display(description="Evidence")
    def evidence_display(self, obj):
        if not obj.evidence:
            return "—"
        url = reverse("simulator:secure_treasury_evidence", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.evidence.name)

    def has_add_permission(self, request):
        # O.3a-5 — Django's own add form stays blocked forever (Fase 0
        # Decision 1, ADMIN_UI.1 pattern): the only entry point is the
        # custom "New Treasury Request" view registered in get_urls()
        # below, which uses TreasuryOperationRequestForm + submit_
        # treasury_request(), not this ModelAdmin's default add machinery.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        # O.3a-5 — anyone who can submit/review/execute a treasury request
        # can also VIEW the (100% read-only, see readonly_fields above)
        # request ledger — needed so the post-submit redirect to this
        # object's own change/detail page (O.3a-5 UX design §6) actually
        # resolves for an operator who only holds can_submit_treasury_
        # request, instead of dead-ending in a 403 right after a
        # successful submission. has_change_permission stays hard-False
        # above regardless, so this never grants edit access — only
        # read-only visibility, the same discipline as every field in
        # readonly_fields already enforces.
        if request.user.has_perm("simulator.can_submit_treasury_request"):
            return True
        if request.user.has_perm("simulator.can_review_treasury_request"):
            return True
        if request.user.has_perm("simulator.can_execute_treasury_request"):
            return True
        # O.3c-5b — a recovery-only operator (holding can_recover_
        # treasury_execution but none of the other three Treasury
        # permissions) must also be able to view this read-only detail
        # page — otherwise the EXECUTING recovery banner and "Mark as
        # FAILED" button built below would be structurally unreachable
        # for the exact role they exist for.
        if request.user.has_perm("simulator.can_recover_treasury_execution"):
            return True
        return super().has_view_permission(request, obj)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        if request.user.has_perm("simulator.can_submit_treasury_request"):
            extra_context["new_treasury_request_url"] = reverse("admin:treasury_request_new")
        # O.3d-4 — dashboard link visible only to the same role that can
        # act on what it shows (Mark as FAILED lives entirely in
        # treasury_request_recover_view(), O.3c-5b, unmodified — this
        # link never grants more than that view already grants).
        if request.user.has_perm("simulator.can_recover_treasury_execution"):
            extra_context["treasury_operational_dashboard_url"] = reverse(
                "admin:treasury_operational_dashboard",
            )
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        """
        O.3b-3 — injects the Approve/Reject header buttons into the
        detail page, exactly the visibility rule frozen in the O.3b
        design: status == PENDING, request.user holds
        can_review_treasury_request, and request.user is not the
        request's own requested_by. Read-only lookup only — no state
        changes happen in this method; approve/reject themselves live
        entirely in treasury_request_approve_view()/
        treasury_request_reject_view() below, which call
        approve_treasury_request()/reject_treasury_request() (O.3b-2)
        unmodified.

        O.3c-5a — same discipline extended to the Execute header
        button: status == APPROVED, request.user holds
        can_execute_treasury_request, request.user is neither
        requested_by nor approved_by, and wallet_transaction is still
        NULL (the exact 5-condition visibility rule frozen in the
        O.3c-5 Fase 0 design). Execution itself lives entirely in
        treasury_request_execute_view() below, which calls
        execute_treasury_request() (O.3c-3) unmodified. The
        EXECUTED/FAILED state panel (also O.3c-5a) needs no extra
        context here — it reads directly off `original` in the
        template, since every field it displays is already a plain
        readonly model field.

        O.3c-5b — same discipline extended once more, this time to the
        EXECUTING recovery banner and its "Mark as FAILED" button.
        inspect_stuck_treasury_execution() (treasury_execution_
        recovery.py, unmodified) is called here strictly to build
        DISPLAY context (case/age/eligible/block_reason) — it is a
        read-only diagnostic by construction (see its own module
        docstring: no .save(), no .update(), never imports
        wallet_ledger.py), so calling it from this read-only view does
        not duplicate or bypass any authorization the mutation itself
        (mark_treasury_execution_failed(), called only from
        treasury_request_recover_view() below) independently
        re-enforces under its own lock. Button visibility mirrors the
        4-condition rule frozen in the O.3c-5 Fase 0 design: permission,
        eligible=True, not requested_by, not approved_by.

        O.3e-3 — Cancel button added with the same discipline, this time
        mirroring cancel_treasury_request()'s (O.3e-2) two-branch
        permission dispatch (self-withdrawal vs. administrative
        cancellation) instead of a single fixed permission. See the
        can_cancel block below for the exact rule.
        """
        extra_context = extra_context or {}
        instance = TreasuryOperationRequest.objects.filter(pk=object_id).first()
        if instance is not None and request.user.is_authenticated:
            can_review = (
                instance.status == TreasuryOperationRequest.ST_PENDING
                and request.user.has_perm("simulator.can_review_treasury_request")
                and instance.requested_by_id != request.user.pk
            )
            if can_review:
                extra_context["show_treasury_review_buttons"] = True
                extra_context["treasury_approve_url"] = reverse(
                    "admin:treasury_request_approve", args=[instance.pk],
                )
                extra_context["treasury_reject_url"] = reverse(
                    "admin:treasury_request_reject", args=[instance.pk],
                )

            # O.3e-3 — Cancel button. Unlike can_review/can_execute above,
            # the permission required is not fixed: it mirrors the
            # self-withdrawal vs. administrative-cancellation dispatch
            # frozen inside cancel_treasury_request() (O.3e-2 Fase 0
            # Decisions 3/4), so this button appears for the request's
            # own requested_by (holding can_submit_treasury_request) as
            # well as for any other holder of can_review_treasury_request
            # — the exact opposite of can_review's "not requested_by"
            # exclusion, since self-cancellation is the intended
            # "self-withdrawal" path here, not a conflict of interest.
            is_own_request = instance.requested_by_id == request.user.pk
            can_cancel = instance.status == TreasuryOperationRequest.ST_PENDING and (
                (is_own_request and request.user.has_perm("simulator.can_submit_treasury_request"))
                or (not is_own_request and request.user.has_perm("simulator.can_review_treasury_request"))
            )
            if can_cancel:
                extra_context["show_treasury_cancel_button"] = True
                extra_context["treasury_cancel_url"] = reverse(
                    "admin:treasury_request_cancel", args=[instance.pk],
                )

            can_execute = (
                instance.status == TreasuryOperationRequest.ST_APPROVED
                and request.user.has_perm("simulator.can_execute_treasury_request")
                and instance.requested_by_id != request.user.pk
                and instance.approved_by_id != request.user.pk
                and instance.wallet_transaction_id is None
            )
            if can_execute:
                extra_context["show_treasury_execute_button"] = True
                extra_context["treasury_execute_url"] = reverse(
                    "admin:treasury_request_execute", args=[instance.pk],
                )

            if instance.status == TreasuryOperationRequest.ST_EXECUTING:
                from .treasury_execution_recovery import inspect_stuck_treasury_execution

                candidate = next(
                    (c for c in inspect_stuck_treasury_execution() if c.instance.pk == instance.pk),
                    None,
                )
                if candidate is not None:
                    extra_context["treasury_recovery_candidate"] = candidate
                    extra_context["treasury_recovery_case_label"] = _treasury_recovery_case_label(
                        candidate.case,
                    )
                    extra_context["treasury_recovery_age_display"] = _treasury_recovery_age_display(
                        candidate.age_seconds,
                    )

                    can_recover = (
                        request.user.has_perm("simulator.can_recover_treasury_execution")
                        and candidate.eligible
                        and instance.requested_by_id != request.user.pk
                        and instance.approved_by_id != request.user.pk
                    )
                    if can_recover:
                        extra_context["show_treasury_recover_button"] = True
                        extra_context["treasury_recover_url"] = reverse(
                            "admin:treasury_request_recover", args=[instance.pk],
                        )
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "new/",
                self.admin_site.admin_view(self.treasury_request_new_view),
                name="treasury_request_new",
            ),
            path(
                "<int:pk>/approve/",
                self.admin_site.admin_view(self.treasury_request_approve_view),
                name="treasury_request_approve",
            ),
            path(
                "<int:pk>/reject/",
                self.admin_site.admin_view(self.treasury_request_reject_view),
                name="treasury_request_reject",
            ),
            path(
                "<int:pk>/execute/",
                self.admin_site.admin_view(self.treasury_request_execute_view),
                name="treasury_request_execute",
            ),
            path(
                "<int:pk>/recover/",
                self.admin_site.admin_view(self.treasury_request_recover_view),
                name="treasury_request_recover",
            ),
            path(
                "<int:pk>/cancel/",
                self.admin_site.admin_view(self.treasury_request_cancel_view),
                name="treasury_request_cancel",
            ),
            path(
                "operational-dashboard/",
                self.admin_site.admin_view(self.treasury_operational_dashboard_view),
                name="treasury_operational_dashboard",
            ),
            path(
                "operational-dashboard/data/",
                self.admin_site.admin_view(self.treasury_operational_dashboard_data),
                name="treasury_operational_dashboard_data",
            ),
        ]
        return custom + urls

    def treasury_request_new_view(self, request):
        """
        O.3a-5 — the only entry point that can create a
        TreasuryOperationRequest. Follows the same shape as every other
        custom admin view in this file (Control Center, Shadow Exposure,
        Dealing Desk): get_urls() + self.admin_site.admin_view(...) +
        a plain method rendering a template in simulator/templates/admin/.

        Permission is checked HERE (view layer) and, independently,
        again inside submit_treasury_request() (service layer,
        O.3a-4) — the two-layer defense explicitly required when O.3a-4
        was approved: this view does not assume the service will catch
        a caller that forgot to check.

        POST validation and creation are delegated entirely to
        TreasuryOperationRequestForm (O.3a-3) and submit_treasury_
        request() (O.3a-4) — no validation or persistence logic is
        duplicated here.
        """
        import json

        from django.core.exceptions import PermissionDenied

        from .forms import TreasuryOperationRequestForm
        from .treasury_requests import TREASURY_SUBMIT_PERMISSION, submit_treasury_request

        if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

        if request.method == "POST":
            form = TreasuryOperationRequestForm(request.POST, request.FILES)
            if form.is_valid():
                instance = submit_treasury_request(form, request=request)
                messages.success(
                    request,
                    f"✓ Treasury Request #{instance.pk} creada — estado: "
                    f"{instance.status}. Pendiente de revisión.",
                )
                return redirect("admin:simulator_treasuryoperationrequest_change", instance.pk)
            messages.error(request, "⚠ Revisa los campos marcados antes de continuar.")
        else:
            form = TreasuryOperationRequestForm()

        context = dict(
            self.admin_site.each_context(request),
            title="New Treasury Request",
            form=form,
            wallet_data_json=json.dumps(_treasury_wallet_confirmation_data()),
            cancel_url=reverse("admin:simulator_treasuryoperationrequest_changelist"),
        )
        return render(request, "admin/treasury_request_new.html", context)

    def treasury_request_approve_view(self, request, pk):
        """
        O.3b-3 — confirmation screen for approve_treasury_request()
        (O.3b-2). This method does not reimplement any review logic —
        it only renders a read-only summary on GET, and on POST calls
        approve_treasury_request() exactly as built there, unmodified.

        Visibility/permission is checked here (view layer) AND,
        independently, again inside approve_treasury_request() (service
        layer) — same two-layer defense already used by
        treasury_request_new_view()/submit_treasury_request() (O.3a-5).
        A status recheck also happens here on GET (so a stale
        confirmation screen is never shown for an already-processed
        request), on top of the service's own recheck under lock.
        """
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        from .treasury_requests import (
            TREASURY_REVIEW_PERMISSION,
            TreasuryRequestNotPending,
            approve_treasury_request,
        )

        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = TreasuryOperationRequest.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("Treasury request not found.")

        if instance.requested_by_id == request.user.pk:
            raise PermissionDenied("No puedes aprobar tu propia solicitud.")

        change_url = reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk])

        if instance.status != TreasuryOperationRequest.ST_PENDING:
            messages.warning(
                request,
                f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                f"{instance.status}) — no se puede revisar.",
            )
            return redirect(change_url)

        if request.method == "POST":
            review_notes = request.POST.get("review_notes", "")
            try:
                approve_treasury_request(instance, request=request, review_notes=review_notes)
            except TreasuryRequestNotPending:
                messages.warning(
                    request,
                    f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                    f"{instance.status}) — no se puede revisar.",
                )
            except TreasuryOperationRequest.DoesNotExist:
                raise Http404("Treasury request not found.")
            else:
                messages.success(request, f"✓ Treasury Request #{instance.pk} aprobada.")
            return redirect(change_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Approve Treasury Request #{instance.pk}",
            instance=instance,
            reviewing_as=request.user,
            cancel_url=change_url,
        )
        return render(request, "admin/treasury_request_approve.html", context)

    def treasury_request_reject_view(self, request, pk):
        """
        O.3b-3 — confirmation screen for reject_treasury_request()
        (O.3b-2). Same discipline as treasury_request_approve_view()
        above: no review logic is reimplemented here.

        rejection_reason is validated for non-emptiness HERE, before
        the service is ever called — this makes reject_treasury_
        request()'s own ValueError branch structurally unreachable
        from this call site (same pattern already used by
        treasury_request_new_view() making submit_treasury_request()'s
        ValueError/ValidationError branches unreachable), so the
        operator sees a normal re-rendered form with an inline error
        instead of a raw exception.
        """
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        from .treasury_requests import (
            TREASURY_REVIEW_PERMISSION,
            TreasuryRequestNotPending,
            reject_treasury_request,
        )

        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = TreasuryOperationRequest.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("Treasury request not found.")

        if instance.requested_by_id == request.user.pk:
            raise PermissionDenied("No puedes rechazar tu propia solicitud.")

        change_url = reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk])

        if instance.status != TreasuryOperationRequest.ST_PENDING:
            messages.warning(
                request,
                f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                f"{instance.status}) — no se puede revisar.",
            )
            return redirect(change_url)

        if request.method == "POST":
            rejection_reason = request.POST.get("rejection_reason", "")
            review_notes = request.POST.get("review_notes", "")

            if not rejection_reason.strip():
                messages.error(request, "⚠ Rejection Reason es obligatorio.")
                context = dict(
                    self.admin_site.each_context(request),
                    title=f"Reject Treasury Request #{instance.pk}",
                    instance=instance,
                    reviewing_as=request.user,
                    cancel_url=change_url,
                    rejection_reason=rejection_reason,
                    review_notes=review_notes,
                )
                return render(request, "admin/treasury_request_reject.html", context)

            try:
                reject_treasury_request(
                    instance, rejection_reason, request=request, review_notes=review_notes,
                )
            except TreasuryRequestNotPending:
                messages.warning(
                    request,
                    f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                    f"{instance.status}) — no se puede revisar.",
                )
            except TreasuryOperationRequest.DoesNotExist:
                raise Http404("Treasury request not found.")
            else:
                messages.success(request, f"✓ Treasury Request #{instance.pk} rechazada.")
            return redirect(change_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Reject Treasury Request #{instance.pk}",
            instance=instance,
            reviewing_as=request.user,
            cancel_url=change_url,
        )
        return render(request, "admin/treasury_request_reject.html", context)

    def treasury_request_execute_view(self, request, pk):
        """
        O.3c-5a — confirmation screen for execute_treasury_request()
        (O.3c-3). This method does not reimplement any financial or
        state-machine logic — it only renders a read-only summary on
        GET, and on POST calls execute_treasury_request() exactly as
        built there, unmodified. InsufficientFunds is imported only
        to translate it into an operator-facing message — this view
        never imports or calls credit_wallet()/debit_wallet()
        directly, and never moves money itself.

        Visibility/permission is checked here (view layer) AND,
        independently, again inside execute_treasury_request() (service
        layer) — same two-layer defense already used by
        treasury_request_approve_view()/treasury_request_reject_view()
        (O.3b-3). A status recheck also happens here on GET (so a stale
        confirmation screen is never shown for an already-processed
        request), on top of the service's own recheck under lock.
        """
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        from .treasury_requests import (
            TREASURY_EXECUTE_PERMISSION,
            TreasuryRequestExecutionInconsistent,
            TreasuryRequestNotApproved,
            TreasuryRequestSelfExecutionDenied,
            execute_treasury_request,
        )
        from .wallet_ledger import InsufficientFunds

        if not request.user.has_perm(TREASURY_EXECUTE_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_EXECUTE_PERMISSION}")

        instance = TreasuryOperationRequest.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("Treasury request not found.")

        if instance.requested_by_id == request.user.pk or instance.approved_by_id == request.user.pk:
            raise PermissionDenied(
                "No puedes ejecutar una solicitud que tú mismo solicitaste o aprobaste."
            )

        change_url = reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk])

        if instance.status != TreasuryOperationRequest.ST_APPROVED:
            messages.warning(
                request,
                f"⚠ Esta solicitud ya no está aprobada (estado actual: "
                f"{instance.status}) — no se puede ejecutar.",
            )
            return redirect(change_url)

        if request.method == "POST":
            execution_notes = request.POST.get("execution_notes", "")
            try:
                executed = execute_treasury_request(
                    instance, request=request, execution_notes=execution_notes,
                )
            except TreasuryRequestNotApproved:
                messages.warning(
                    request,
                    f"⚠ Esta solicitud ya no está aprobada (estado actual: "
                    f"{instance.status}) — no se puede ejecutar.",
                )
            except TreasuryRequestSelfExecutionDenied:
                raise PermissionDenied(
                    "No puedes ejecutar una solicitud que tú mismo solicitaste o aprobaste."
                )
            except (InsufficientFunds, ValueError):
                messages.error(
                    request,
                    f"⚠ La ejecución falló — fondos insuficientes o monto inválido. "
                    f"La solicitud #{instance.pk} fue marcada como FAILED. "
                    "Ver failure_reason en el detalle.",
                )
            except TreasuryRequestExecutionInconsistent:
                messages.error(
                    request,
                    "⚠ La ejecución no pudo completarse — el estado de la solicitud "
                    "cambió de forma inesperada durante el procesamiento. Requiere "
                    "investigación operativa (ver AuditLog). No se movieron fondos "
                    "en este intento.",
                )
            except TreasuryOperationRequest.DoesNotExist:
                raise Http404("Treasury request not found.")
            else:
                wtx = executed.wallet_transaction
                messages.success(
                    request,
                    f"✓ Treasury Request #{executed.pk} ejecutada — WalletTransaction "
                    f"#{wtx.pk} creada ({wtx.get_tx_type_display()}). Nuevo balance: "
                    f"{wtx.balance_after}.",
                )
            return redirect(change_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Execute Treasury Request #{instance.pk}",
            instance=instance,
            executing_as=request.user,
            cancel_url=change_url,
        )
        return render(request, "admin/treasury_request_execute.html", context)

    def treasury_request_recover_view(self, request, pk):
        """
        O.3c-5b — confirmation screen for mark_treasury_execution_failed()
        (O.3c-4c). This method does not reimplement any classification
        or state-machine logic — inspect_stuck_treasury_execution() is
        called only to render the read-only summary (case/age/eligible/
        block_reason) on GET, and on POST mark_treasury_execution_
        failed() is called exactly as built there, unmodified. Neither
        call ever touches wallet_ledger.py, credit_wallet(), or
        debit_wallet() — recovery is incident response for a row that
        never moved money, never a financial operation itself (same
        invariant treasury_execution_recovery.py's own module docstring
        states).

        Visibility/permission is checked here (view layer) AND,
        independently, again inside mark_treasury_execution_failed()
        (service layer) — same two-layer defense already used by every
        other Treasury admin view in this file. A status/eligibility
        recheck also happens here on GET (so a stale confirmation
        screen is never shown for a request that changed state or is
        no longer eligible), on top of the service's own pre-lock and
        post-lock rechecks.

        recovery_reason is validated for non-emptiness HERE, before the
        service is ever called — makes mark_treasury_execution_failed()'s
        own ValueError branch structurally unreachable from this call
        site, same pattern already used by treasury_request_reject_view()
        making reject_treasury_request()'s ValueError branch unreachable.
        """
        from django.core.exceptions import PermissionDenied
        from django.db import Error as DjangoDBError
        from django.http import Http404

        from .treasury_execution_recovery import (
            TREASURY_RECOVER_PERMISSION,
            TreasuryRequestSelfRecoveryDenied,
            inspect_stuck_treasury_execution,
            mark_treasury_execution_failed,
        )
        from .treasury_requests import TreasuryRequestExecutionInconsistent

        if not request.user.has_perm(TREASURY_RECOVER_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_RECOVER_PERMISSION}")

        instance = TreasuryOperationRequest.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("Treasury request not found.")

        if instance.requested_by_id == request.user.pk or instance.approved_by_id == request.user.pk:
            raise PermissionDenied(
                "No puedes recuperar una solicitud que tú mismo solicitaste o aprobaste."
            )

        change_url = reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk])

        if instance.status != TreasuryOperationRequest.ST_EXECUTING:
            messages.warning(
                request,
                f"⚠ Esta solicitud ya no está en EXECUTING (estado actual: "
                f"{instance.status}) — no requiere recuperación.",
            )
            return redirect(change_url)

        candidate = next(
            (c for c in inspect_stuck_treasury_execution() if c.instance.pk == instance.pk), None,
        )
        if candidate is None:
            messages.warning(
                request,
                "⚠ Esta solicitud ya no está en EXECUTING — no requiere recuperación.",
            )
            return redirect(change_url)

        if not candidate.eligible:
            messages.warning(
                request,
                f"⚠ Esta solicitud no es elegible para recuperación: {candidate.block_reason}",
            )
            return redirect(change_url)

        if request.method == "POST":
            recovery_reason = request.POST.get("recovery_reason", "")
            if not recovery_reason.strip():
                messages.error(request, "⚠ Recovery Reason es obligatorio.")
                context = dict(
                    self.admin_site.each_context(request),
                    title=f"Recover Stuck Execution #{instance.pk}",
                    instance=instance,
                    candidate=candidate,
                    case_label=_treasury_recovery_case_label(candidate.case),
                    age_display=_treasury_recovery_age_display(candidate.age_seconds),
                    recovering_as=request.user,
                    cancel_url=change_url,
                    recovery_reason=recovery_reason,
                )
                return render(request, "admin/treasury_request_recover.html", context)

            try:
                mark_treasury_execution_failed(
                    instance, request=request, recovery_reason=recovery_reason,
                )
            except TreasuryRequestSelfRecoveryDenied:
                raise PermissionDenied(
                    "No puedes recuperar una solicitud que tú mismo solicitaste o aprobaste."
                )
            except TreasuryRequestExecutionInconsistent:
                messages.error(
                    request,
                    "⚠ La recuperación no pudo completarse — el estado de la solicitud "
                    "cambió de forma inesperada durante el procesamiento (o ya no es "
                    "elegible). Requiere investigación operativa (ver AuditLog).",
                )
            except DjangoDBError:
                messages.error(
                    request,
                    "⚠ La solicitud está siendo procesada activamente por otra "
                    "operación — intenta nuevamente en unos segundos.",
                )
            except TreasuryOperationRequest.DoesNotExist:
                raise Http404("Treasury request not found.")
            else:
                messages.success(
                    request,
                    f"✓ Treasury Request #{instance.pk} marcada como FAILED por "
                    "recuperación manual. No se movieron fondos.",
                )
            return redirect(change_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Recover Stuck Execution #{instance.pk}",
            instance=instance,
            candidate=candidate,
            case_label=_treasury_recovery_case_label(candidate.case),
            age_display=_treasury_recovery_age_display(candidate.age_seconds),
            recovering_as=request.user,
            cancel_url=change_url,
        )
        return render(request, "admin/treasury_request_recover.html", context)

    def treasury_request_cancel_view(self, request, pk):
        """
        O.3e-3 — confirmation screen for cancel_treasury_request()
        (O.3e-2). This method does not reimplement any state-machine
        logic — it only renders a read-only summary on GET, and on POST
        calls cancel_treasury_request() exactly as built there,
        unmodified.

        Unlike approve/reject/execute/recover, the required permission
        here is NOT fixed — it depends on whether request.user is the
        request's own requested_by (self-withdrawal, needs
        TREASURY_SUBMIT_PERMISSION) or someone else (administrative
        cancellation, needs TREASURY_REVIEW_PERMISSION), mirroring the
        dispatch frozen inside cancel_treasury_request() itself (O.3e-2
        Fase 0 Decisions 3/4). Both the button visibility below (in
        change_view()) and the permission check here re-derive the same
        two-branch rule independently of the service's own re-check
        under lock — same two-layer defense already used by every other
        Treasury admin view in this file.

        cancellation_reason is optional (O.3e-2 Fase 0 decision — never
        required for either actor), so there is no inline "field
        required" re-render branch here, unlike
        treasury_request_reject_view()'s mandatory rejection_reason.
        """
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        from .treasury_requests import (
            TREASURY_REVIEW_PERMISSION,
            TREASURY_SUBMIT_PERMISSION,
            TreasuryRequestNotPending,
            cancel_treasury_request,
        )

        instance = TreasuryOperationRequest.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("Treasury request not found.")

        is_self_withdrawal = instance.requested_by_id == request.user.pk
        if is_self_withdrawal:
            if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
                raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")
        else:
            if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
                raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        change_url = reverse("admin:simulator_treasuryoperationrequest_change", args=[instance.pk])

        if instance.status != TreasuryOperationRequest.ST_PENDING:
            messages.warning(
                request,
                f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                f"{instance.status}) — no se puede cancelar.",
            )
            return redirect(change_url)

        if request.method == "POST":
            cancellation_reason = request.POST.get("cancellation_reason", "")
            try:
                cancel_treasury_request(
                    instance, request=request, cancellation_reason=cancellation_reason,
                )
            except TreasuryRequestNotPending:
                messages.warning(
                    request,
                    f"⚠ Esta solicitud ya no está pendiente (estado actual: "
                    f"{instance.status}) — no se puede cancelar.",
                )
            except TreasuryOperationRequest.DoesNotExist:
                raise Http404("Treasury request not found.")
            else:
                messages.success(request, f"✓ Treasury Request #{instance.pk} cancelada.")
            return redirect(change_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Cancel Treasury Request #{instance.pk}",
            instance=instance,
            cancelling_as=request.user,
            is_self_withdrawal=is_self_withdrawal,
            cancel_url=change_url,
        )
        return render(request, "admin/treasury_request_cancel.html", context)

    # ── O.3d-4 — Treasury Operational Dashboard ───────────────────────

    def treasury_operational_dashboard_data(self, request):
        """
        JSON live-data endpoint. Redis-cached for 30 s (same pattern as
        BrokerRevenueSnapshotAdmin.broker_control_data() — raw redis-py,
        try/except non-fatal on both get and set, DB is the only source
        of truth); falls back to a direct DB read if Redis is
        unavailable either way.

        Access restricted to simulator.can_recover_treasury_execution —
        narrower than this ModelAdmin's own has_view_permission(), by
        design: this dashboard exists for the role that can act on a
        stuck execution, not every Treasury role that can merely view
        the changelist. Superusers pass via Django's own has_perm()
        contract, no special-casing here.

        recover_url is computed HERE, per request, AFTER the
        (potentially cached, potentially shared-across-viewers) payload
        is obtained — never baked into the cached JSON itself. See
        _compute_treasury_operational_dashboard_data()'s own docstring
        for why: self-conflict with requested_by/approved_by depends on
        request.user, which must never be memoized across different
        viewers sharing one 30s cache entry.
        """
        import json

        from django.conf import settings as _s
        from django.core.exceptions import PermissionDenied
        from django.http import JsonResponse

        if not request.user.has_perm("simulator.can_recover_treasury_execution"):
            raise PermissionDenied(
                "Missing permission: simulator.can_recover_treasury_execution"
            )

        _KEY = "trx:treasury:operational_dashboard:v1"
        _TTL = 30

        def _redis():
            import redis as _r
            url = (getattr(_s, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
            return _r.from_url(url, socket_connect_timeout=1, socket_timeout=1)

        payload = None
        try:
            cached = _redis().get(_KEY)
            if cached:
                payload = json.loads(cached)
        except Exception:
            payload = None

        if payload is None:
            payload = _compute_treasury_operational_dashboard_data()
            try:
                _redis().setex(_KEY, _TTL, json.dumps(payload))
            except Exception:
                pass

        for c in payload["candidates"]:
            c["recover_url"] = None
            if (
                c["eligible"]
                and c["requested_by_id"] != request.user.pk
                and c["approved_by_id"] != request.user.pk
            ):
                c["recover_url"] = reverse(
                    "admin:treasury_request_recover", args=[c["treasury_operation_request_id"]],
                )

        return JsonResponse(payload)

    def treasury_operational_dashboard_view(self, request):
        """HTML shell for the Treasury Operational Dashboard. JS polls
        /data/ every 10 s. Same permission gate as the data endpoint
        above — checked again here independently, since this is a
        separate URL a browser could hit directly."""
        from django.core.exceptions import PermissionDenied

        if not request.user.has_perm("simulator.can_recover_treasury_execution"):
            raise PermissionDenied(
                "Missing permission: simulator.can_recover_treasury_execution"
            )

        context = dict(
            self.admin_site.each_context(request),
            title="Treasury Operational Dashboard",
            data_url=reverse("admin:treasury_operational_dashboard_data"),
            changelist_url=reverse("admin:simulator_treasuryoperationrequest_changelist"),
            history_limit=TREASURY_DASHBOARD_HISTORY_LIMIT,
        )
        return render(request, "admin/treasury_operational_dashboard.html", context)


# ─────────────────────────────────────────────
# Audit Log — read-only
# ─────────────────────────────────────────────

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display  = ("created_at", "event_type", "action", "user", "account", "ip", "request_id")
    list_filter   = ("event_type",)
    search_fields = ("event_type", "action", "ip", "request_id", "user__username")
    readonly_fields = (
        "event_type", "action", "user", "account",
        "ip", "endpoint", "method", "request_id", "detail", "created_at",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # superuser can purge stale rows if needed


@admin.register(BrokerAuditEvent)
class BrokerAuditEventAdmin(admin.ModelAdmin):
    """
    AUDIT-01 (post-review correction) — real append-only protection.
    BrokerAuditEvent rows are written exclusively by
    simulator/broker_audit.py; nothing may add, change, or delete a row
    through this admin, individually or in bulk. account/trade are
    already SET_NULL on delete (see the model) — deleting the
    underlying TradingAccount/Trade nulls the FK here, it never removes
    the historical event.
    """
    list_display  = (
        "timestamp", "severity", "category", "event_type",
        "actor_type", "account", "symbol", "description",
    )
    list_filter   = ("category", "severity", "actor_type")
    search_fields = (
        "event_type", "description", "symbol", "request_id",
        # AUDIT-02 — Payments domain search surface
        "funded_payout_request__id", "deposit__id", "correlation_id",
        # AUDIT-03 — Compliance domain search surface
        "user__username",
    )
    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)
    readonly_fields = (
        "event_id", "event_type", "category", "severity", "timestamp",
        "actor_type", "actor_id", "account", "trade", "symbol",
        # AUDIT-02
        "funded_payout_request", "deposit", "correlation_id", "event_version",
        # AUDIT-03
        "user",
        "description", "metadata", "source_module", "request_id",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        # Belt-and-suspenders: has_delete_permission=False already hides
        # delete_selected, but strip it from the actions dict explicitly
        # so its absence is a structural guarantee, not an inference.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(LiquidityProvider)
class LiquidityProviderAdmin(admin.ModelAdmin):
    """
    BOOK-05a — plain CRUD, same pattern as BrokerSpreadConfigAdmin/
    InstrumentAdmin: a LiquidityProvider is staff configuration, not a
    recorded fact, so no permission overrides — normal Django model
    permissions apply.
    """
    list_display = (
        "name", "enabled", "simulated_spread_markup_pips", "max_capacity_usd", "updated_at",
    )
    list_filter = ("enabled",)
    search_fields = ("name",)
    list_editable = ("enabled", "simulated_spread_markup_pips", "max_capacity_usd")
    readonly_fields = ("created_at", "updated_at")


@admin.register(LiquidityDecision)
class LiquidityDecisionAdmin(admin.ModelAdmin):
    """
    BOOK-05a — read-only visibility surface, same append-only protection
    pattern as RoutingDecisionAdmin. LiquidityDecision rows are written
    exclusively by simulator/liquidity_engine.py (BOOK-05c, not yet
    implemented — this table is empty until then); nothing may add,
    change, or delete a row through this admin, individually or in bulk.
    routing_decision/provider/position are already SET_NULL on delete
    (see the model) — deleting the underlying RoutingDecision/
    LiquidityProvider/Position nulls the FK here, it never removes the
    historical simulation.
    """
    list_display = (
        "decision_id", "symbol", "provider", "exposure_usd", "simulated_cost", "decided_at",
    )
    list_filter = ("provider", "symbol")
    search_fields = (
        "decision_id", "routing_decision__decision_id", "position__id",
    )
    ordering = ("-decided_at", "-id")
    readonly_fields = [f.name for f in LiquidityDecision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(LiquidityLedger)
class LiquidityLedgerAdmin(admin.ModelAdmin):
    """
    BOOK-05d.1 — read-only visibility surface, same append-only
    protection pattern as LiquidityDecisionAdmin/RoutingDecisionAdmin.
    LiquidityLedger rows will be written exclusively by
    simulator/liquidity_ledger.py (BOOK-05d.2, not yet implemented —
    this table is empty until then); nothing may add, change, or delete
    a row through this admin, individually or in bulk. source_trade/
    liquidity_decision are already SET_NULL on delete (see the model) —
    deleting the underlying Trade/LiquidityDecision nulls the FK here,
    it never removes the historical simulated ledger entry.
    """
    list_display = (
        "id", "symbol", "simulated_pnl", "source_trade", "liquidity_decision", "created_at",
    )
    list_filter = ("symbol",)
    search_fields = (
        "source_trade__id", "liquidity_decision__decision_id",
    )
    ordering = ("-created_at", "-id")
    readonly_fields = [f.name for f in LiquidityLedger._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(DealingDeskDecision)
class DealingDeskDecisionAdmin(admin.ModelAdmin):
    """
    BOOK-06a — read-only visibility surface, same append-only protection
    pattern as LiquidityDecisionAdmin/LiquidityLedgerAdmin. DealingDeskDecision
    rows will be written exclusively by a future BOOK-06 writer (not yet
    implemented — this table is empty until then); nothing may add,
    change, or delete a row through this admin, individually or in bulk.
    routing_decision/position/liquidity_decision are already SET_NULL on
    delete (see the model) — deleting the underlying row nulls the FK
    here, it never removes the historical classification.
    """
    list_display = (
        "decision_id", "symbol", "is_simulated_hedge", "routing_profile_snapshot", "decided_at",
    )
    list_filter = ("is_simulated_hedge", "routing_profile_snapshot", "symbol")
    search_fields = (
        "decision_id", "routing_decision__decision_id", "position__id",
    )
    ordering = ("-decided_at", "-id")
    readonly_fields = [f.name for f in DealingDeskDecision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(RoutingDecision)
class RoutingDecisionAdmin(admin.ModelAdmin):
    """
    BOOK-04e — read-only visibility surface, same append-only protection
    pattern as BrokerAuditEventAdmin. RoutingDecision rows are written
    exclusively by simulator/routing_engine.py; nothing may add, change,
    or delete a row through this admin, individually or in bulk.
    account/position are already SET_NULL on delete (see the model) —
    deleting the underlying TradingAccount/Position nulls the FK here,
    it never removes the historical decision.
    """
    list_display = (
        "decision_id", "book", "reason_code", "account", "position", "decided_at",
    )
    list_filter = ("book", "reason_code")
    search_fields = (
        "decision_id", "account__user__username", "position__id",
    )
    date_hierarchy = "decided_at"
    ordering = ("-decided_at", "-id")
    readonly_fields = [f.name for f in RoutingDecision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


# ─────────────────────────────────────────────
# Broker Ecosystem Modules
# ─────────────────────────────────────────────

@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display  = ('title', 'currency', 'country', 'event_date', 'impact_badge', 'actual', 'forecast', 'published')
    list_filter   = ('impact', 'currency', 'published')
    search_fields = ('title', 'currency', 'country')
    ordering      = ('event_date',)
    list_editable = ('published',)
    date_hierarchy = 'event_date'
    fieldsets = (
        (None, {'fields': ('title', 'currency', 'country', 'event_date', 'impact', 'published')}),
        ('Datos', {'fields': ('actual', 'forecast', 'previous')}),
    )

    @admin.display(description='Impact')
    def impact_badge(self, obj):
        colors = {'HIGH': ('#4a0000', '#ef5350'), 'MEDIUM': ('#2a1a00', '#ff9800'), 'LOW': ('#0a2a1a', '#26a69a')}
        bg, fg = colors.get(obj.impact, ('#222', '#aaa'))
        return _badge(obj.impact, bg, fg)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display  = ('user', 'code', 'clicks', 'registrations', 'estimated_commission', 'created_at')
    search_fields = ('user__username', 'code')
    readonly_fields = ('code', 'clicks', 'registrations', 'created_at')
    ordering      = ('-created_at',)

    def has_add_permission(self, request):
        return False


@admin.register(Bonus)
class BonusAdmin(admin.ModelAdmin):
    list_display  = ('title', 'bonus_type', 'value', 'active', 'expires_at', 'created_at')
    list_filter   = ('active', 'bonus_type')
    list_editable = ('active',)
    search_fields = ('title', 'description')
    ordering      = ('-created_at',)
    fieldsets = (
        (None, {'fields': ('title', 'description', 'bonus_type', 'value', 'active')}),
        ('Expiración (opcional)', {'fields': ('expires_at',)}),
    )


@admin.register(BrokerDocument)
class BrokerDocumentAdmin(admin.ModelAdmin):
    list_display  = ('title', 'category', 'public', 'created_at')
    list_filter   = ('category', 'public')
    list_editable = ('public',)
    search_fields = ('title', 'description')
    ordering      = ('category', 'title')

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # O.5e-1 — the stock ClearableFileInput widget links straight to
        # `.file.url` (raw MEDIA_URL); this swaps in a widget that links to
        # secure_broker_document_view instead, which re-checks `public`/
        # staff permissions on every request regardless of this link.
        if db_field.name == 'file':
            kwargs['widget'] = broker_document_secure_widget()
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(ExpertAdvisor)
class ExpertAdvisorAdmin(admin.ModelAdmin):
    list_display  = ('name', 'category', 'version', 'active', 'coming_soon', 'download_url', 'created_at')
    list_filter   = ('category', 'active', 'coming_soon')
    list_editable = ('active', 'coming_soon')
    search_fields = ('name', 'description')
    ordering      = ('category', 'name')


# ─────────────────────────────────────────────
# Broker Revenue Ledger
# ─────────────────────────────────────────────

@admin.register(BrokerSpreadConfig)
class BrokerSpreadConfigAdmin(admin.ModelAdmin):
    list_display   = (
        'symbol', 'spread_pips', 'enabled', 'is_dynamic',
        'manual_multiplier', 'manual_expires_at',
        'spread_bounds_enabled', 'min_spread', 'max_spread', 'created_at',
    )
    list_filter    = ('enabled', 'is_dynamic', 'spread_bounds_enabled')
    search_fields  = ('symbol',)
    list_editable  = ('spread_pips', 'enabled', 'is_dynamic', 'spread_bounds_enabled')
    readonly_fields = ('created_at',)
    fields = (
        'symbol', 'spread_pips', 'enabled', 'is_dynamic',
        'manual_multiplier', 'manual_reason', 'manual_expires_at',
        'spread_bounds_enabled', 'min_spread', 'max_spread', 'created_at',
    )
    ordering       = ('symbol',)


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    """
    MD-5 FASE 1 (approved 2026-07-28) — this catalog is administrative
    reference only. `market_data.symbol_specs.SymbolSpec` remains the
    sole runtime authority for every field edited here (pricing,
    contract, margin, market-data routing, trading gate) — see
    docs/MARKET_DATA_ARCHITECTURE.md §5-6 and the MD-5 FASE 0 RFC
    (2026-07-28). Editing a row here has ZERO effect on live trading,
    pricing, risk, or order execution — this was a real, silent
    operator-error vector (no warning existed anywhere in this admin
    before this block), now made explicit via the warning banner below
    on every list/add/change view. Kept editable (not made readonly):
    `Instrument` remains a legitimate reference/staging catalog per the
    FASE 0 recommendation (Option B — code-first stays authoritative,
    no migration to DB-first) — the fix for the risk was visibility,
    not restricting a use case (drift comparison, future planning)
    that genuinely needs edits to keep working.
    """
    _RUNTIME_WARNING = (
        "⚠ Este catálogo es de referencia administrativa únicamente. "
        "NINGÚN campo editado aquí afecta el trading real, el pricing, el "
        "margen ni el riesgo — la fuente de verdad del runtime sigue "
        "siendo market_data.symbol_specs.SymbolSpec (código), no esta "
        "tabla. Ver docs/MARKET_DATA_ARCHITECTURE.md."
    )

    list_display = (
        'symbol', 'display_name', 'asset_class', 'trading_enabled',
        'market_data_provider', 'max_leverage', 'default_spread', 'spread_unit',
    )
    list_filter    = ('asset_class', 'trading_enabled', 'market_data_provider')
    search_fields  = ('symbol', 'display_name')
    ordering       = ('asset_class', 'symbol')
    readonly_fields = ('created_at', 'updated_at')

    def changelist_view(self, request, extra_context=None):
        messages.warning(request, self._RUNTIME_WARNING)
        return super().changelist_view(request, extra_context=extra_context)

    def add_view(self, request, form_url='', extra_context=None):
        messages.warning(request, self._RUNTIME_WARNING)
        return super().add_view(request, form_url, extra_context=extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        messages.warning(request, self._RUNTIME_WARNING)
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    fieldsets = (
        ('Identity', {
            'fields': ('symbol', 'display_name', 'asset_class', 'base_currency', 'quote_currency'),
        }),
        ('Pricing', {
            'fields': ('pip_size', 'tick_size', 'price_decimals'),
        }),
        ('Contract', {
            'fields': ('lot_step', 'min_lot', 'max_lot', 'contract_size'),
        }),
        ('Execution Costs', {
            'fields': ('default_spread', 'spread_unit', 'commission_per_lot', 'commission_pct'),
        }),
        ('Margin & PnL', {
            'fields': ('max_leverage', 'margin_mode', 'pnl_mode'),
        }),
        ('Market Data Routing', {
            'fields': ('market_data_provider', 'provider_symbol'),
        }),
        ('Trading Gate', {
            'fields': ('trading_enabled', 'session'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(BrokerLedger)
class BrokerLedgerAdmin(admin.ModelAdmin):
    # BOOK-02 — source_trade added: the field existed before but was never
    # populated by anything; COUNTERPARTY_PNL is the first entry type that
    # sets it, so it's now worth surfacing in the list view. revenue_type
    # already auto-lists COUNTERPARTY_PNL as a filter option (Django
    # populates list_filter choices from the field's `choices`, not from
    # distinct DB values) and `amount` already renders negative values
    # correctly with no code change (plain Decimal field).
    @admin.display(description="Dashboard")
    def revenue_dashboard_link(self, obj):
        url = reverse("admin:brokerledger_revenue_dashboard")
        return format_html('<a href="{}">→ Revenue Dashboard</a>', url)

    list_display   = ('id', 'revenue_type', 'amount', 'source_account', 'source_trade', 'symbol', 'created_at',
                       'revenue_dashboard_link')
    list_filter    = ('revenue_type', 'created_at')
    search_fields  = ('symbol', 'source_account__id')
    readonly_fields = (
        'id', 'revenue_type', 'amount', 'source_account', 'source_trade',
        'source_ledger', 'symbol', 'meta', 'created_at',
    )
    ordering       = ('-created_at',)
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["dashboard_url"] = reverse("admin:brokerledger_revenue_dashboard")
        return super().changelist_view(request, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        return [
            path(
                "dashboard/",
                self.admin_site.admin_view(self.revenue_dashboard_view),
                name="brokerledger_revenue_dashboard",
            ),
        ] + urls

    def revenue_dashboard_view(self, request):
        import datetime
        from django.db.models.functions import TruncDay
        from django.utils import timezone

        # ── GET filters ───────────────────────────────────────────────
        f_type   = request.GET.get("revenue_type", "").strip()
        f_symbol = request.GET.get("symbol", "").strip()
        f_from   = request.GET.get("date_from", "").strip()
        f_to     = request.GET.get("date_to", "").strip()

        # BOOK-02 — this dashboard predates COUNTERPARTY_PNL and is titled
        # "Revenue": COUNTERPARTY_PNL is B-Book directional PnL (can be
        # negative), not revenue, so it's excluded here to keep existing
        # totals unchanged (it is NOT selectable via f_type on this view
        # either — a dedicated counterparty PnL view is BOOK-03's job).
        qs = BrokerLedger.objects.exclude(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        if f_type:
            qs = qs.filter(revenue_type=f_type)
        if f_symbol:
            qs = qs.filter(symbol=f_symbol)
        if f_from:
            try:
                qs = qs.filter(created_at__date__gte=f_from)
            except Exception:
                f_from = ""
        if f_to:
            try:
                qs = qs.filter(created_at__date__lte=f_to)
            except Exception:
                f_to = ""

        def _f(v):
            return float(v or 0)

        # ── Aggregate summary (filtered) ───────────────────────────────
        totals = qs.aggregate(
            grand_total   = Sum("amount"),
            commission    = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_COMMISSION)),
            spread        = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_SPREAD)),
            challenge_fee = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)),
            withdraw_fee  = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_WITHDRAW_FEE)),
            adjustment    = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_ADJUSTMENT)),
            row_count     = Count("id"),
        )

        # ── Period snapshots (always global — unaffected by user filters) ─
        now_dt    = timezone.now()
        today_s   = now_dt.date().isoformat()
        week_ago  = (now_dt - datetime.timedelta(days=7)).date().isoformat()
        month_ago = (now_dt - datetime.timedelta(days=30)).date().isoformat()
        _base     = BrokerLedger.objects.exclude(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        period    = {
            "today": _f(_base.filter(created_at__date=today_s).aggregate(t=Sum("amount"))["t"]),
            "week":  _f(_base.filter(created_at__date__gte=week_ago).aggregate(t=Sum("amount"))["t"]),
            "month": _f(_base.filter(created_at__date__gte=month_ago).aggregate(t=Sum("amount"))["t"]),
        }

        # ── By revenue type ────────────────────────────────────────────
        _label_map = dict(BrokerLedger.REVENUE_CHOICES)
        by_type = [
            {
                "key":   r["revenue_type"],
                "label": _label_map.get(r["revenue_type"], r["revenue_type"]),
                "total": _f(r["total"]),
                "count": r["count"],
            }
            for r in qs.values("revenue_type")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")
        ]

        # ── By symbol (top 15) ─────────────────────────────────────────
        by_symbol = [
            {"symbol": r["symbol"], "total": _f(r["total"]), "count": r["count"]}
            for r in qs.exclude(symbol__isnull=True).exclude(symbol="")
                       .values("symbol")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")[:15]
        ]

        # ── By account (top 15) ────────────────────────────────────────
        by_account = [
            {
                "account_id": r["source_account_id"],
                "username":   r["source_account__user__username"] or f"#{r['source_account_id']}",
                "total":      _f(r["total"]),
                "count":      r["count"],
            }
            for r in qs.exclude(source_account__isnull=True)
                       .values("source_account_id", "source_account__user__username")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")[:15]
        ]

        # ── Daily trend (last 30 days, filtered) ───────────────────────
        daily = [
            {
                "day":   r["day"].strftime("%Y-%m-%d"),
                "total": _f(r["total"]),
                "count": r["count"],
            }
            for r in qs.filter(created_at__date__gte=month_ago)
                       .annotate(day=TruncDay("created_at"))
                       .values("day")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("day")
        ]

        # ── Dropdown helpers ────────────────────────────────────────────
        all_symbols = list(
            BrokerLedger.objects.exclude(symbol__isnull=True).exclude(symbol="")
                                .values_list("symbol", flat=True)
                                .distinct().order_by("symbol")
        )

        context = dict(
            self.admin_site.each_context(request),
            title           = "Broker Revenue Dashboard",
            grand_total     = _f(totals["grand_total"]),
            t_commission    = _f(totals["commission"]),
            t_spread        = _f(totals["spread"]),
            t_challenge     = _f(totals["challenge_fee"]),
            t_withdraw      = _f(totals["withdraw_fee"]),
            t_adjustment    = _f(totals["adjustment"]),
            row_count       = totals["row_count"] or 0,
            period          = period,
            by_type         = by_type,
            by_symbol       = by_symbol,
            by_account      = by_account,
            daily           = daily,
            all_symbols     = all_symbols,
            # BOOK-02 — COUNTERPARTY_PNL is excluded from this dashboard's
            # queryset (see above); omit it from the filter dropdown too so
            # there's no selectable option that always yields zero rows.
            revenue_choices = [c for c in BrokerLedger.REVENUE_CHOICES if c[0] != BrokerLedger.REV_COUNTERPARTY_PNL],
            f_type          = f_type,
            f_symbol        = f_symbol,
            f_from          = f_from,
            f_to            = f_to,
            changelist_url  = reverse("admin:simulator_brokerledger_changelist"),
        )
        return render(request, "admin/broker_revenue_dashboard.html", context)


# ─────────────────────────────────────────────
# Broker Analytics Engine
# ─────────────────────────────────────────────

def _build_equity_svg(snapshots: list, width: int = 800, height: int = 120) -> str:
    """
    Generate an inline SVG polyline from a list of BrokerRevenueSnapshot objects.
    x-axis = time-proportional, y-axis = total_revenue normalized to [10, 110] px.
    Returns empty string if fewer than 2 points.
    """
    if len(snapshots) < 2:
        return ""
    values = [float(s.total_revenue) for s in snapshots]
    min_v, max_v = min(values), max(values)
    v_range = max_v - min_v
    pad_top, pad_bot = 10, 10
    draw_h = height - pad_top - pad_bot
    n = len(snapshots)

    pts = []
    for i, v in enumerate(values):
        x = round(i / (n - 1) * width, 2)
        y = round(
            pad_top + (1.0 - (v - min_v) / v_range) * draw_h
            if v_range > 0 else height / 2,
            2,
        )
        pts.append((x, y))

    poly   = " ".join(f"{x},{y}" for x, y in pts)
    fill_p = f"0,{height} {poly} {width},{height}"

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'preserveAspectRatio="none" style="width:100%;height:{height}px;display:block;">'
        f'<defs>'
        f'<linearGradient id="rg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#e8c84a" stop-opacity="0.22"/>'
        f'<stop offset="100%" stop-color="#e8c84a" stop-opacity="0.02"/>'
        f'</linearGradient>'
        f'</defs>'
        f'<polygon points="{fill_p}" fill="url(#rg)"/>'
        f'<polyline points="{poly}" fill="none" stroke="#e8c84a" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def _downsample(rows: list, max_pts: int = 300) -> list:
    """Thin a list to at most max_pts evenly-spaced entries."""
    if len(rows) <= max_pts:
        return rows
    step = len(rows) / max_pts
    return [rows[int(i * step)] for i in range(max_pts)]


def _compute_control_data() -> dict:
    """
    Build the live broker control center JSON payload.
    Pure read — no writes to any financial table.
    Called by broker_control_data() which wraps it with Redis caching.
    """
    import datetime
    from django.db.models import Sum, Q
    from django.utils import timezone

    def _f(v): return float(v or 0)

    # BOOK-03 — today_start now comes from the single shared UTC period
    # builder (simulator/broker_pnl.py) instead of a locally-rolled
    # .replace(hour=0,...). Mathematically identical (UTC midnight of the
    # current day) — not a semantic change, just deduplication (FASE 4).
    # week_start/month_start/day_24h stay as local ROLLING windows
    # (now-7d/now-30d/now-24h) for this trend view — deliberately NOT
    # redirected to the engine's calendar week/month periods, which are a
    # different concept (Monday-anchored / 1st-of-month-anchored) and
    # would silently change these numbers; see BOOK-03 ENTREGA FASE 4.
    from .broker_pnl import utc_period_window, PERIOD_TODAY
    now          = timezone.now()
    today_start, _ = utc_period_window(PERIOD_TODAY, now=now)
    yest_start   = today_start - datetime.timedelta(days=1)
    week_start   = now - datetime.timedelta(days=7)
    month_start  = now - datetime.timedelta(days=30)
    day_24h      = now - datetime.timedelta(hours=24)

    # Q1 — O(1) via index: lifetime cumulative totals
    rev_snap = BrokerRevenueSnapshot.objects.order_by("-taken_at").first()

    # Q2 — O(1) via index: live operational state
    eq_snap = BrokerEquitySnapshot.objects.order_by("-taken_at").first()

    # Q3 — O(1) + ≤5 rows: per-symbol exposure from latest BrokerSnapshot
    bk_snap   = BrokerSnapshot.objects.order_by("-created_at").first()
    dangerous = []
    if bk_snap:
        for e in (
            SymbolExposure.objects
            .filter(snapshot=bk_snap)
            .order_by("-net_usd")[:5]
        ):
            dangerous.append({
                "symbol":            e.symbol,
                "net_usd":           float(e.net_usd),
                "is_high_risk":      e.is_high_risk,
                "unrealized_pnl":    float(e.unrealized_pnl),
                "concentration_pct": float(e.concentration_pct),
            })

    # Q4 — indexed range scans, 5 aggregates in one pass each
    # BOOK-02 — excludes COUNTERPARTY_PNL (B-Book directional PnL, can be
    # negative) from this "revenue" base so t_today/t_week/top_symbols/
    # top_accounts keep their pre-BOOK-02 meaning. A unified broker PnL
    # view (fee revenue + counterparty result) is BOOK-03's job.
    bl = BrokerLedger.objects.exclude(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
    today_agg = bl.filter(created_at__gte=today_start).aggregate(
        total     = Sum("amount"),
        spread    = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_SPREAD)),
        comm      = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_COMMISSION)),
        challenge = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_CHALLENGE_FEE)),
        withdraw  = Sum("amount", filter=Q(revenue_type=BrokerLedger.REV_WITHDRAW_FEE)),
    )
    t_yesterday = _f(bl.filter(created_at__gte=yest_start, created_at__lt=today_start)
                       .aggregate(t=Sum("amount"))["t"])
    t_week  = _f(bl.filter(created_at__gte=week_start).aggregate(t=Sum("amount"))["t"])
    t_month = _f(bl.filter(created_at__gte=month_start).aggregate(t=Sum("amount"))["t"])

    # Q5 — bounded by 24h range: top 5 symbols
    top_symbols = list(
        bl.filter(created_at__gte=day_24h, symbol__isnull=False)
        .exclude(symbol="")
        .values("symbol")
        .annotate(rev=Sum("amount"))
        .order_by("-rev")[:5]
    )

    # Q6 — bounded by 24h range: top 5 accounts
    top_accounts = list(
        bl.filter(created_at__gte=day_24h, source_account__isnull=False)
        .values("source_account_id", "source_account__user__username")
        .annotate(rev=Sum("amount"))
        .order_by("-rev")[:5]
    )

    # Derived scalars
    t_today     = _f(today_agg["total"])
    t_lifetime  = float(rev_snap.total_revenue)    if rev_snap else 0.0
    t_spread_all = float(rev_snap.total_spread)    if rev_snap else 0.0
    t_comm_all   = float(rev_snap.total_commission) if rev_snap else 0.0

    growth_pct = round((t_today - t_yesterday) / t_yesterday * 100, 1) if t_yesterday > 0 else 0.0

    sc_sum     = t_spread_all + t_comm_all
    spread_pct = round(t_spread_all / sc_sum * 100, 1) if sc_sum > 0 else 0.0
    comm_pct   = round(t_comm_all   / sc_sum * 100, 1) if sc_sum > 0 else 0.0

    # BOOK-03 — Broker PnL decomposition (B-book counter-party model).
    # Single source of truth: simulator/broker_pnl.py, reading BrokerLedger
    # directly (never re-deriving from BrokerRevenueSnapshot, which only
    # ever tracked fee revenue — see that model's docstring). Replaces the
    # old net_broker_pnl = t_lifetime(fee revenue only) + unrealized_risk,
    # which silently omitted the realized counterparty result entirely
    # (BOOK-01 finding) and mislabeled fee revenue as "realized broker PnL".
    from .broker_pnl import broker_pnl_for_period, PERIOD_LIFETIME
    lifetime_pnl = broker_pnl_for_period(PERIOD_LIFETIME, now=now)
    realized_fee_revenue     = float(lifetime_pnl.fee_revenue)
    realized_counterparty_pnl = float(lifetime_pnl.counterparty_pnl)
    realized_adjustments     = float(lifetime_pnl.adjustments)
    realized_net_pnl         = float(lifetime_pnl.broker_net_pnl)
    coverage_pct             = lifetime_pnl.coverage_pct
    historical_incomplete    = lifetime_pnl.historical_incomplete

    # Unrealized: broker's simulated counter-party risk on currently OPEN
    # positions (exposure_engine, routing-filtered — a separate, documented
    # BOOK-01 finding, not something BOOK-03 changes). Kept strictly apart
    # from the realized figures above — never summed into realized_net_pnl.
    unrealized_counterparty_risk = float(bk_snap.broker_pnl_unrealized) if bk_snap else 0.0
    projected_net_including_open_positions = realized_net_pnl + unrealized_counterparty_risk

    # Back-compat aliases (old field names some earlier iteration of this
    # view/template referenced) — now correctly sourced instead of being
    # fee-revenue-only. Prefer the explicit realized_*/unrealized_*/
    # projected_* names above for anything new.
    unrealized_risk = unrealized_counterparty_risk
    net_broker_pnl  = projected_net_including_open_positions

    # Daily pace — extrapolate today's revenue to EOD
    elapsed_s = max(1, (now - today_start).total_seconds())
    pace_eod  = round(t_today * (86400 / elapsed_s), 2) if t_today > 0 else 0.0

    snap_age_s = int((now - rev_snap.taken_at).total_seconds()) if rev_snap else -1

    # RISK-01 — live open-position exposure, single source of truth
    # (simulator/broker_exposure.py). Kept in its OWN block, separate from
    # "ops" above (which stays sourced from BrokerEquitySnapshot/
    # snapshots.py — untouched, including that path's known contract_size
    # bug, see broker_exposure.py's module docstring) and from "broker_pnl"
    # (BOOK-03, realized-only). projected_broker_result combines BOOK-03's
    # realized_net_pnl with RISK-01's live unrealized counterparty PnL —
    # the two numbers are never merged under an ambiguous name.
    from .broker_exposure import broker_exposure_snapshot
    live_exposure = broker_exposure_snapshot()
    projected_broker_result = realized_net_pnl + float(live_exposure.broker_unrealized_counterparty_pnl)

    # RISK-03 — Broker Health, single source of truth
    # (simulator/broker_alerts.py). Pure observation — never blocks
    # anything, just surfaces what collect_risk_alerts() already found.
    from .broker_alerts import broker_health_summary
    broker_health = broker_health_summary()

    # AUDIT-01 (post-review correction) — this dashboard computation is
    # READ-ONLY with respect to the audit trail. It used to also call
    # broker_audit.record_active_alerts() here, which meant an alert was
    # only ever persisted if a staff member happened to have this
    # dashboard open — a GET/poll should never be what decides whether
    # broker history exists. Alert observations are now persisted
    # exclusively by tasks.py's observe_broker_risk_alerts_task (a
    # periodic Celery task calling broker_audit.observe_broker_alerts()),
    # entirely outside this request path. _compute_control_data() only
    # ever READS BrokerAuditEvent below (recent_audit_events), never
    # writes one.
    from . import broker_audit as _audit

    return {
        "ts":         now.isoformat(),
        "snap_age_s": snap_age_s,
        "revenue": {
            "today":      t_today,
            "yesterday":  t_yesterday,
            "week":       t_week,
            "month":      t_month,
            "lifetime":   t_lifetime,
            "spread":     t_spread_all,
            "commission": t_comm_all,
            "challenge":  _f(today_agg["challenge"]),
            "withdraw":   _f(today_agg["withdraw"]),
            "growth_pct": growth_pct,
        },
        "ops": {
            "active_accounts":  eq_snap.active_accounts   if eq_snap else 0,
            "open_positions":   eq_snap.open_positions    if eq_snap else 0,
            "net_exposure_usd": float(eq_snap.net_exposure_usd) if eq_snap else 0.0,
            "gross_long":       float(eq_snap.gross_long_usd)   if eq_snap else 0.0,
            "gross_short":      float(eq_snap.gross_short_usd)  if eq_snap else 0.0,
        },
        "broker_pnl": {
            # BOOK-03 — full breakdown, correctly labeled. "realized"/
            # "unrealized_risk"/"net" are kept for any old consumer but now
            # point at the CORRECT (net, not fee-only) figures.
            "realized_fee_revenue":     realized_fee_revenue,
            "realized_counterparty_pnl": realized_counterparty_pnl,
            "realized_adjustments":     realized_adjustments,
            "realized_net_pnl":         realized_net_pnl,
            "unrealized_counterparty_risk": unrealized_counterparty_risk,
            "projected_net_including_open_positions": projected_net_including_open_positions,
            "coverage_pct":             coverage_pct,
            "historical_incomplete":    historical_incomplete,
            # Back-compat aliases — "realized" now means the real net
            # realized figure, NOT fee revenue (BOOK-01/BOOK-03 fix).
            "realized":        realized_net_pnl,
            "unrealized_risk": unrealized_counterparty_risk,
            "net":             projected_net_including_open_positions,
        },
        "top_symbols": [
            {"symbol": r["symbol"], "rev_24h": float(r["rev"])}
            for r in top_symbols
        ],
        "top_accounts": [
            {
                "account_id":     r["source_account_id"],
                "account_number": r["source_account__user__username"] or f"#{r['source_account_id']}",
                "rev_24h":        float(r["rev"]),
            }
            for r in top_accounts
        ],
        "dangerous_exposure": dangerous,
        "spread_commission_ratio": {
            "spread_pct":     spread_pct,
            "commission_pct": comm_pct,
        },
        "daily_rev_today": {
            "amount":   t_today,
            "pace_eod": pace_eod,
        },
        "risk_exposure": {
            # RISK-01 — live, correctly-computed (qty * price * contract_size,
            # fresh-price-only) open-position exposure. Separate from "ops"
            # and "broker_pnl" above by design (FASE 8 — never one ambiguous
            # number for realized + unrealized + notional).
            "open_position_count": live_exposure.open_position_count,
            "account_count":       live_exposure.account_count,
            "symbol_count":        live_exposure.symbol_count,
            "long_quantity":       float(live_exposure.long_quantity),
            "short_quantity":      float(live_exposure.short_quantity),
            "gross_quantity":      float(live_exposure.gross_quantity),
            "net_quantity":        float(live_exposure.net_quantity),
            "long_notional":       float(live_exposure.long_notional),
            "short_notional":      float(live_exposure.short_notional),
            "gross_notional":      float(live_exposure.gross_notional),
            "net_notional":        float(live_exposure.net_notional),
            "trader_unrealized_pnl": float(live_exposure.trader_unrealized_pnl),
            "broker_unrealized_counterparty_pnl": float(live_exposure.broker_unrealized_counterparty_pnl),
            "margin_used":         float(live_exposure.margin_used),
            "largest_symbol":      live_exposure.largest_symbol,
            "largest_symbol_gross_notional": float(live_exposure.largest_symbol_gross_notional),
            "pricing_coverage_pct": float(live_exposure.pricing_coverage_pct),
            "unpriced_position_count": live_exposure.unpriced_position_count,
            # BOOK-03 realized_net_pnl + RISK-01 unrealized counterparty PnL.
            "projected_broker_result": projected_broker_result,
        },
        "broker_health": broker_health,
        # AUDIT-01 — FASE 8: simple recent-events view, no dashboard
        # redesign. Read-only; broker_audit.recent_events() is a plain
        # ordered query, nothing computed or cached here.
        "recent_audit_events": [
            {
                "timestamp":   e.timestamp.isoformat(),
                "severity":    e.severity,
                "category":    e.category,
                "event_type":  e.event_type,
                "account_id":  e.account_id,
                "symbol":      e.symbol,
                "description": e.description,
            }
            for e in _audit.recent_events(25)
        ],
    }


@admin.register(BrokerRevenueSnapshot)
class BrokerRevenueSnapshotAdmin(admin.ModelAdmin):

    @admin.display(description="Total Revenue")
    def total_col(self, obj):
        return format_html(
            '<span style="color:#e8c84a;font-weight:700">${}</span>',
            f"{float(obj.total_revenue):,.2f}",
        )

    @admin.display(description="Period Revenue")
    def period_col(self, obj):
        v = float(obj.period_revenue)
        color = "#27ae60" if v > 0 else "#888"
        return format_html(
            '<span style="color:{};font-weight:700">{}</span>',
            color, f"+${v:,.4f}",
        )

    @admin.display(description="Net Exposure")
    def exposure_col(self, obj):
        v = float(obj.net_exposure_usd)
        color = "#ef5350" if abs(v) > 10000 else "#e67e22" if abs(v) > 5000 else "#27ae60"
        return format_html(
            '<span style="color:{};font-weight:700">{}</span>',
            color, f"${v:+,.2f}",
        )

    @admin.display(description="Analytics")
    def analytics_link(self, obj):
        url = reverse("admin:brokerrevsnap_analytics")
        return format_html('<a href="{}">→ Broker Analytics</a>', url)

    @admin.display(description="Control Center")
    def control_center_link(self, obj):
        url = reverse("admin:brokerrevsnap_control")
        return format_html('<a href="{}">→ Broker Control Center</a>', url)

    list_display    = ("taken_at", "total_col", "period_col",
                       "active_accounts", "open_positions", "exposure_col",
                       "analytics_link", "control_center_link")
    list_filter     = ("taken_at",)
    date_hierarchy  = "taken_at"
    ordering        = ("-taken_at",)
    readonly_fields = (
        "taken_at",
        "total_revenue", "total_commission", "total_spread",
        "total_challenge", "total_withdraw", "total_adjustment",
        "period_revenue", "period_commission", "period_spread",
        "active_accounts", "open_positions",
        "net_exposure_usd", "gross_long_usd", "gross_short_usd",
    )

    def has_add_permission(self, request):        return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["analytics_url"]      = reverse("admin:brokerrevsnap_analytics")
        extra_context["control_center_url"] = reverse("admin:brokerrevsnap_control")
        return super().changelist_view(request, extra_context)

    def get_urls(self):
        return [
            path(
                "analytics/",
                self.admin_site.admin_view(self.analytics_view),
                name="brokerrevsnap_analytics",
            ),
            path(
                "broker-control/",
                self.admin_site.admin_view(self.broker_control_view),
                name="brokerrevsnap_control",
            ),
            path(
                "broker-control/data/",
                self.admin_site.admin_view(self.broker_control_data),
                name="brokerrevsnap_control_data",
            ),
        ] + super().get_urls()

    # ── Broker Control Center ─────────────────────────────────────────────────

    def broker_control_data(self, request):
        """JSON live-data endpoint. Redis-cached for 30 s; falls back to DB if Redis is down."""
        import json
        from django.http import JsonResponse
        from django.conf import settings as _s

        _KEY = "trx:broker:control"
        _TTL = 30

        def _redis():
            import redis as _r
            url = (getattr(_s, "REDIS_URL", "") or "").strip() or "redis://127.0.0.1:6379/0"
            return _r.from_url(url, socket_connect_timeout=1, socket_timeout=1)

        # 1. Cache hit?
        try:
            cached = _redis().get(_KEY)
            if cached:
                return JsonResponse(json.loads(cached))
        except Exception:
            pass

        # 2. Compute from DB
        payload = _compute_control_data()

        # 3. Write cache (non-fatal if Redis is unavailable)
        try:
            _redis().setex(_KEY, _TTL, json.dumps(payload))
        except Exception:
            pass

        return JsonResponse(payload)

    def broker_control_view(self, request):
        """HTML shell for the Broker Control Center. JS polls /data/ every 10 s."""
        context = dict(
            self.admin_site.each_context(request),
            title             = "Broker Control Center",
            data_url          = reverse("admin:brokerrevsnap_control_data"),
            analytics_url     = reverse("admin:brokerrevsnap_analytics"),
            changelist_url    = reverse("admin:simulator_brokerrevenuesnapshot_changelist"),
            revenue_url       = reverse("admin:brokerledger_revenue_dashboard"),
            exposure_url      = reverse("admin:broker_live_analytics"),
            is_superuser      = request.user.is_superuser,
            shadow_exposure_url = reverse("admin:broker_shadow_exposure"),
        )
        return render(request, "admin/broker_control_center.html", context)

    def analytics_view(self, request):
        import datetime
        from django.db.models import Sum, Count, Q
        from django.db.models.functions import TruncDay
        from django.utils import timezone
        from django.utils.html import mark_safe

        now_dt    = timezone.now()
        today     = now_dt.date()
        yesterday = today - datetime.timedelta(days=1)
        week_ago  = today - datetime.timedelta(days=7)
        month_ago = today - datetime.timedelta(days=30)

        def _f(v): return float(v or 0)

        # ── Window toggle ─────────────────────────────────────────────
        window = request.GET.get("window", "24h")
        if window not in ("24h", "7d", "30d"):
            window = "24h"
        _window_delta = {"24h": datetime.timedelta(hours=24),
                          "7d": datetime.timedelta(days=7),
                          "30d": datetime.timedelta(days=30)}
        curve_since = now_dt - _window_delta[window]
        curve_qs = list(
            BrokerRevenueSnapshot.objects
            .filter(taken_at__gte=curve_since)
            .order_by("taken_at")
        )
        curve_pts    = _downsample(curve_qs)
        equity_svg   = mark_safe(_build_equity_svg(curve_pts))
        curve_first  = curve_pts[0].taken_at.strftime("%Y-%m-%d %H:%M") if curve_pts else "—"
        curve_last   = curve_pts[-1].taken_at.strftime("%Y-%m-%d %H:%M") if curve_pts else "—"
        curve_start_val = float(curve_pts[0].total_revenue)  if curve_pts else 0.0
        curve_end_val   = float(curve_pts[-1].total_revenue) if curve_pts else 0.0
        curve_delta     = curve_end_val - curve_start_val

        # ── KPI cards ─────────────────────────────────────────────────
        # BOOK-02 — excludes COUNTERPARTY_PNL (B-Book directional PnL, can
        # be negative) so this "revenue" view keeps its pre-BOOK-02 meaning.
        bl = BrokerLedger.objects.exclude(revenue_type=BrokerLedger.REV_COUNTERPARTY_PNL)
        t_today     = _f(bl.filter(created_at__date=today).aggregate(t=Sum("amount"))["t"])
        t_yesterday = _f(bl.filter(created_at__date=yesterday).aggregate(t=Sum("amount"))["t"])
        t_week      = _f(bl.filter(created_at__date__gte=week_ago).aggregate(t=Sum("amount"))["t"])
        t_month     = _f(bl.filter(created_at__date__gte=month_ago).aggregate(t=Sum("amount"))["t"])
        latest_snap = BrokerRevenueSnapshot.objects.order_by("-taken_at").first()
        t_lifetime  = float(latest_snap.total_revenue) if latest_snap else 0.0
        growth_pct  = (
            round((t_today - t_yesterday) / t_yesterday * 100, 1)
            if t_yesterday > 0 else None
        )

        # ── Revenue by type (all time) ────────────────────────────────
        _label = dict(BrokerLedger.REVENUE_CHOICES)
        by_type = [
            {
                "key":   r["revenue_type"],
                "label": _label.get(r["revenue_type"], r["revenue_type"]),
                "total": _f(r["total"]),
                "count": r["count"],
            }
            for r in bl.values("revenue_type")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")
        ]
        t_spread_all     = next((r["total"] for r in by_type if r["key"] == "SPREAD"),     0.0)
        t_commission_all = next((r["total"] for r in by_type if r["key"] == "COMMISSION"), 0.0)
        sc_sum           = t_spread_all + t_commission_all
        spread_pct       = round(t_spread_all     / sc_sum * 100, 1) if sc_sum > 0 else 0.0
        commission_pct   = round(t_commission_all / sc_sum * 100, 1) if sc_sum > 0 else 0.0

        # ── Top 10 symbols (30d) ──────────────────────────────────────
        top_symbols = [
            {"symbol": r["symbol"], "total": _f(r["total"]), "count": r["count"]}
            for r in bl.filter(created_at__date__gte=month_ago)
                       .exclude(symbol__isnull=True).exclude(symbol="")
                       .values("symbol")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")[:10]
        ]

        # ── Top 10 accounts (30d) ─────────────────────────────────────
        top_accounts = [
            {
                "account_id": r["source_account_id"],
                "username":   r["source_account__user__username"] or f"#{r['source_account_id']}",
                "total":      _f(r["total"]),
                "count":      r["count"],
            }
            for r in bl.exclude(source_account__isnull=True)
                       .filter(created_at__date__gte=month_ago)
                       .values("source_account_id", "source_account__user__username")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("-total")[:10]
        ]

        # ── Daily revenue bars (last 30 days) ─────────────────────────
        daily = [
            {"day": r["day"].strftime("%Y-%m-%d"), "total": _f(r["total"]), "count": r["count"]}
            for r in bl.filter(created_at__date__gte=month_ago)
                       .annotate(day=TruncDay("created_at"))
                       .values("day")
                       .annotate(total=Sum("amount"), count=Count("id"))
                       .order_by("day")
        ]
        daily_max = max((d["total"] for d in daily), default=1.0) or 1.0

        # ── Exposure × Revenue merge ───────────────────────────────────
        rev_by_sym = {
            r["symbol"]: _f(r["total"])
            for r in bl.filter(created_at__date__gte=month_ago)
                       .exclude(symbol__isnull=True).exclude(symbol="")
                       .values("symbol")
                       .annotate(total=Sum("amount"))
        }
        latest_bsnap = BrokerSnapshot.objects.order_by("-created_at").first()
        exp_by_sym   = {}
        if latest_bsnap:
            for e in SymbolExposure.objects.filter(snapshot=latest_bsnap).select_related():
                exp_by_sym[e.symbol] = e

        exp_rev = []
        for sym in sorted(set(rev_by_sym) | set(exp_by_sym)):
            e = exp_by_sym.get(sym)
            exp_rev.append({
                "symbol":           sym,
                "revenue_30d":      rev_by_sym.get(sym, 0.0),
                "net_usd":          float(e.net_usd)           if e else 0.0,
                "concentration_pct": float(e.concentration_pct) if e else 0.0,
                "unrealized_pnl":   float(e.unrealized_pnl)    if e else 0.0,
                "is_high_risk":     e.is_high_risk              if e else False,
            })
        exp_rev.sort(key=lambda r: (-r["revenue_30d"], -abs(r["net_usd"])))

        # ── Operational state from latest revenue snapshot ─────────────
        ops = {
            "active_accounts": latest_snap.active_accounts  if latest_snap else "—",
            "open_positions":  latest_snap.open_positions    if latest_snap else "—",
            "net_exposure":    float(latest_snap.net_exposure_usd) if latest_snap else 0.0,
            "gross_long":      float(latest_snap.gross_long_usd)   if latest_snap else 0.0,
            "gross_short":     float(latest_snap.gross_short_usd)  if latest_snap else 0.0,
        }

        context = dict(
            self.admin_site.each_context(request),
            title          = "Broker Analytics",
            # KPI
            t_today        = t_today,
            t_yesterday    = t_yesterday,
            t_week         = t_week,
            t_month        = t_month,
            t_lifetime     = t_lifetime,
            growth_pct     = growth_pct,
            # Revenue breakdown
            by_type        = by_type,
            spread_pct     = spread_pct,
            commission_pct = commission_pct,
            t_spread_all   = t_spread_all,
            t_commission_all = t_commission_all,
            # Rankings
            top_symbols    = top_symbols,
            top_accounts   = top_accounts,
            sym_max        = top_symbols[0]["total"] if top_symbols else 1.0,
            acc_max        = top_accounts[0]["total"] if top_accounts else 1.0,
            # Trend
            daily          = daily,
            daily_max      = daily_max,
            # Equity curve
            equity_svg     = equity_svg,
            window         = window,
            curve_first    = curve_first,
            curve_last     = curve_last,
            curve_delta    = curve_delta,
            curve_pts_count = len(curve_pts),
            # Exposure × Revenue
            exp_rev        = exp_rev,
            # Ops
            ops            = ops,
            changelist_url = reverse("admin:simulator_brokerrevenuesnapshot_changelist"),
            revenue_url    = reverse("admin:brokerledger_revenue_dashboard"),
            exposure_url   = reverse("admin:broker_live_analytics"),
        )
        return render(request, "admin/broker_analytics_dashboard.html", context)


# ─────────────────────────────────────────────
# Challenge Control Panel
# ─────────────────────────────────────────────

_ENROLLMENT_STATUS_COLORS = {
    ChallengeEnrollment.ST_PHASE_1:   ("#0d2b45", "#29b6f6"),
    ChallengeEnrollment.ST_PHASE_2:   ("#1a2b0d", "#66bb6a"),
    ChallengeEnrollment.ST_FUNDED:    ("#1a2b0d", "#26a69a"),
    ChallengeEnrollment.ST_FAILED:    ("#4a0000", "#ef5350"),
    ChallengeEnrollment.ST_WITHDRAWN: ("#1a1a1a", "#888888"),
}


def _enroll_status_badge(status: str) -> str:
    bg, fg = _ENROLLMENT_STATUS_COLORS.get(status, ("#1a1a1a", "#888888"))
    return format_html(
        '<span style="background:{};color:{};padding:2px 8px;border-radius:3px;'
        'font-size:.7rem;font-weight:700;">{}</span>',
        bg, fg, status,
    )


def _account_link(account) -> str:
    if account is None:
        return "—"
    url = reverse("admin:simulator_tradingaccount_change", args=[account.pk])
    return format_html('<a href="{}">#{}  {}</a>', url, account.pk, account.phase or account.account_type)


@admin.action(description="Activate selected enrollments (create Phase 1 account)")
def activate_enrollments(modeladmin, request, queryset):
    ok = failed = 0
    for enrollment in queryset.select_related("product", "phase1_account"):
        try:
            challenge_engine.activate_challenge_enrollment(enrollment)
            ok += 1
        except Exception as exc:
            messages.error(request, f"Enrollment #{enrollment.pk}: {exc}")
            failed += 1
    if ok:
        messages.success(request, f"{ok} enrollment(s) activated.")
    if failed:
        messages.warning(request, f"{failed} enrollment(s) failed — see errors above.")


@admin.action(description="Evaluate selected enrollments now (auto-advance or fail)")
def evaluate_enrollments_now(modeladmin, request, queryset):
    counts = {challenge_engine.PASSED: 0, challenge_engine.FAILED: 0, challenge_engine.IN_PROGRESS: 0}
    for enrollment in queryset:
        try:
            result = challenge_engine.evaluate_enrollment_now(enrollment.pk)
            counts[result.status] += 1
        except Exception as exc:
            messages.error(request, f"Enrollment #{enrollment.pk}: {exc}")
    if counts[challenge_engine.PASSED]:
        messages.success(request, f"{counts[challenge_engine.PASSED]} enrollment(s) passed and advanced.")
    if counts[challenge_engine.FAILED]:
        messages.warning(request, f"{counts[challenge_engine.FAILED]} enrollment(s) failed.")
    if counts[challenge_engine.IN_PROGRESS]:
        messages.info(request, f"{counts[challenge_engine.IN_PROGRESS]} enrollment(s) still in progress.")


@admin.register(ChallengeProduct)
class ChallengeProductAdmin(admin.ModelAdmin):
    list_display  = ("name", "tier", "price_usd", "account_size", "phases_summary",
                      "profit_split_pct", "spread_markup_pips", "commission_per_lot",
                      "is_active", "created_at")
    list_filter   = ("is_active", "tier")
    search_fields = ("name",)
    readonly_fields = ("created_at",)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    fieldsets = (
        (None, {
            "fields": ("name", "tier", "price_usd", "account_size", "profit_split_pct",
                       "max_lot_size", "max_open_positions", "is_active", "created_at"),
        }),
        ("Phase 1 Rules", {
            "fields": ("p1_profit_target_pct", "p1_max_drawdown_pct", "p1_max_daily_loss_pct",
                       "p1_min_trading_days", "p1_max_duration_days"),
        }),
        ("Phase 2 Rules", {
            "fields": ("p2_profit_target_pct", "p2_max_drawdown_pct", "p2_max_daily_loss_pct",
                       "p2_min_trading_days", "p2_max_duration_days"),
        }),
        ("Commercial Pricing (SPREAD-04)", {
            "fields": ("spread_markup_pips", "commission_per_lot", "commission_pct",
                       "min_spread_pips", "max_spread_pips"),
            "description": (
                "Applied to every TradingAccount created from this product "
                "(Phase 1, Phase 2, and Funded) — frozen at creation time, "
                "editing this afterward never retroactively changes existing "
                "accounts. min/max_spread_pips optionally override the "
                "symbol's own BrokerSpreadConfig floor/ceiling; leave blank "
                "to use the symbol's default."
            ),
        }),
    )

    @admin.display(description="Phases")
    def phases_summary(self, obj):
        return format_html(
            "P1: {}% / P2: {}%",
            obj.p1_profit_target_pct,
            obj.p2_profit_target_pct,
        )


@admin.register(AccountProduct)
class AccountProductAdmin(admin.ModelAdmin):
    """SPREAD-04 — previously not registered at all; AccountProduct rows
    could only be created/edited via seed_account_products.py or direct DB
    access. Registered here so commercial pricing (typical_spread_pips/
    commission_per_lot/commission_pct, already existing fields) is
    admin-editable like ChallengeProduct's."""

    list_display  = ("name", "code", "product_type", "family", "typical_spread_pips",
                      "commission_per_lot", "commission_pct", "is_active", "is_popular", "created_at")
    list_filter   = ("family", "product_type", "is_active", "is_popular")
    search_fields = ("name", "code")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    fieldsets = (
        (None, {
            "fields": ("name", "code", "product_type", "family", "platform_label",
                       "description", "is_active", "created_at"),
        }),
        ("Economics", {
            "fields": ("min_deposit", "default_balance", "max_leverage",
                       "typical_spread_pips", "commission_per_lot", "commission_pct",
                       "spread_markup"),
            "description": (
                "Applied to every TradingAccount created from this product — "
                "frozen at creation time (see simulator/commercial_pricing.py); "
                "editing this afterward never retroactively changes existing "
                "accounts."
            ),
        }),
        ("Risk Parameters", {
            "fields": ("allowed_symbols", "max_lot_size", "margin_call_level", "stopout_level",
                       "max_margin_per_trade_pct", "max_total_margin_pct"),
            "description": (
                "max_margin_per_trade_pct / max_total_margin_pct (O.6c-1e): frozen at "
                "account creation, same as the other Risk Parameters above — editing "
                "these afterward never retroactively changes existing accounts. "
                "Defaults (10.00 / 50.00) match the platform's historical global caps."
            ),
        }),
        ("Display", {
            "fields": ("features", "is_popular", "sort_order"),
        }),
    )


@admin.register(ChallengeEnrollment)
class ChallengeEnrollmentAdmin(admin.ModelAdmin):
    list_display  = ("__str__", "user", "product", "status_badge", "phase1_link",
                     "phase2_link", "funded_link", "enrolled_at")
    list_filter   = ("status", "product__tier")
    search_fields = ("user__username", "user__email", "product__name")
    readonly_fields = ("enrolled_at", "phase1_passed_at", "phase2_passed_at", "funded_at",
                       "status", "failed_at_phase", "failure_reason")
    actions = [activate_enrollments, evaluate_enrollments_now]

    fieldsets = (
        (None, {
            "fields": ("user", "product", "deposit"),
        }),
        ("Accounts", {
            "fields": ("phase1_account", "phase2_account", "funded_account"),
        }),
        ("Status", {
            "fields": ("status", "failed_at_phase", "failure_reason"),
        }),
        ("Timeline", {
            "fields": ("enrolled_at", "phase1_passed_at", "phase2_passed_at", "funded_at"),
        }),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        return _enroll_status_badge(obj.status)

    @admin.display(description="Phase 1")
    def phase1_link(self, obj):
        return _account_link(obj.phase1_account)

    @admin.display(description="Phase 2")
    def phase2_link(self, obj):
        return _account_link(obj.phase2_account)

    @admin.display(description="Funded")
    def funded_link(self, obj):
        return _account_link(obj.funded_account)


@admin.register(FundedConfig)
class FundedConfigAdmin(admin.ModelAdmin):
    list_display  = ("enrollment", "funded_account_link", "funded_type",
                     "profit_split_pct", "min_payout_usd", "payout_cycle_days",
                     "min_trading_days", "is_active", "created_at")
    list_filter   = ("funded_type", "is_active")
    readonly_fields = ("enrollment", "funded_type", "profit_split_pct", "min_payout_usd",
                       "min_trading_days", "payout_cycle_days", "max_monthly_drawdown_pct",
                       "is_active", "created_at")

    @admin.display(description="Funded Account")
    def funded_account_link(self, obj):
        account = obj.enrollment.funded_account if obj.enrollment_id else None
        return _account_link(account)


# ─────────────────────────────────────────────
# KYC
# ─────────────────────────────────────────────

@admin.register(KYCProfile)
class KYCProfileAdmin(admin.ModelAdmin):
    list_display   = ("user", "status", "legal_name", "country",
                      "document_type", "submitted_at", "reviewed_at", "reviewed_by")
    list_filter    = ("status", "country", "document_type")
    search_fields  = ("user__username", "user__email", "legal_name")
    readonly_fields = ("user", "created_at", "updated_at", "submitted_at",
                       "reviewed_at", "reviewed_by")
    ordering       = ("-submitted_at", "-created_at")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # O.5e-1 — the stock ClearableFileInput widget links straight to
        # `.url` (raw MEDIA_URL); this swaps in a widget that links to
        # secure_kyc_media_view instead, which re-checks owner/staff
        # permissions on every request regardless of this link.
        if db_field.name in ("document_front", "document_back", "selfie"):
            kwargs["widget"] = kyc_secure_widget(db_field.name)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    fieldsets = (
        ("Identity", {
            "fields": ("user", "status", "legal_name", "country"),
        }),
        ("Document", {
            "fields": ("document_type", "document_number",
                       "document_front", "document_back", "selfie"),
        }),
        ("Review", {
            "fields": ("reviewed_by", "reviewed_at", "rejection_reason"),
        }),
        ("Timestamps", {
            "fields": ("submitted_at", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.action(description="Approve selected KYC profiles")
    def approve_kyc(self, request, queryset):
        """
        AUDIT-03 — one transaction.atomic()+select_for_update() per row,
        never one lock held across the whole batch (avoids a deadlock risk
        if two bulk actions run with overlapping-but-differently-ordered
        selections). The status recheck happens AFTER acquiring the lock,
        reading a fresh row — not the possibly-stale object read before the
        loop. Only the transaction that actually wins the race reaches the
        mutation, the audit event, and the email; the loser's recheck
        makes it `continue` before any of those. Emails are queued after
        every lock in this action has been released, over the list of
        profiles that actually transitioned — never over the original
        admin selection, which may include rows a race discarded.
        """
        from django.utils import timezone
        from . import broker_audit as _audit

        ids = list(queryset.values_list("pk", flat=True))
        approved = []
        _now = timezone.now()
        for kyc_id in ids:
            with transaction.atomic():
                kyc = KYCProfile.objects.select_for_update().get(pk=kyc_id)
                if kyc.status != KYCProfile.STATUS_PENDING:
                    continue  # lost the race, or wasn't pending — same silent skip as before
                kyc.status           = KYCProfile.STATUS_APPROVED
                kyc.reviewed_at      = _now
                kyc.reviewed_by      = request.user
                kyc.rejection_reason = ""
                kyc.save(update_fields=["status", "reviewed_at", "reviewed_by", "rejection_reason"])
                _audit.record_compliance_event(
                    event_type=_audit.EV_KYC_APPROVED, severity=_audit.Severity.INFO,
                    actor_id=request.user.pk, user=kyc.user,
                    source_module="simulator.admin",
                    description=f"KYC profile #{kyc.pk} approved",
                    metadata={
                        "kyc_profile_id": kyc.pk,
                        "status_before": "pending",
                        "status_after": "approved",
                    },
                )
            approved.append(kyc)

        for kyc in approved:
            try:
                from .kyc_emails import send_kyc_approved_email
                send_kyc_approved_email(kyc)
            except Exception as mail_exc:
                _wlog.warning("[admin] kyc approved email failed kyc=%d: %s", kyc.pk, mail_exc)
        self.message_user(request, f"{len(approved)} KYC profile(s) approved.")

    @admin.action(description="Reject selected KYC profiles")
    def reject_kyc(self, request, queryset):
        """AUDIT-03 — same per-row locking discipline as approve_kyc() above."""
        from django.utils import timezone
        from . import broker_audit as _audit

        ids = list(queryset.values_list("pk", flat=True))
        rejected = []
        _now = timezone.now()
        for kyc_id in ids:
            with transaction.atomic():
                kyc = KYCProfile.objects.select_for_update().get(pk=kyc_id)
                if kyc.status != KYCProfile.STATUS_PENDING:
                    continue
                kyc.status      = KYCProfile.STATUS_REJECTED
                kyc.reviewed_at = _now
                kyc.reviewed_by = request.user
                kyc.save(update_fields=["status", "reviewed_at", "reviewed_by"])
                _audit.record_compliance_event(
                    event_type=_audit.EV_KYC_REJECTED, severity=_audit.Severity.WARNING,
                    actor_id=request.user.pk, user=kyc.user,
                    source_module="simulator.admin",
                    description=f"KYC profile #{kyc.pk} rejected",
                    metadata={
                        "kyc_profile_id": kyc.pk,
                        "status_before": "pending",
                        "status_after": "rejected",
                        "rejection_reason": (kyc.rejection_reason or "")[
                            :_audit.KYC_REJECTION_REASON_MAX_LENGTH
                        ],
                    },
                )
            rejected.append(kyc)

        for kyc in rejected:
            try:
                from .kyc_emails import send_kyc_rejected_email
                send_kyc_rejected_email(kyc)
            except Exception as mail_exc:
                _wlog.warning("[admin] kyc rejected email failed kyc=%d: %s", kyc.pk, mail_exc)
        self.message_user(request, f"{len(rejected)} KYC profile(s) rejected.")

    actions = ["approve_kyc", "reject_kyc"]


# ─────────────────────────────────────────────
# Support Tickets
# ─────────────────────────────────────────────

@admin.action(description="📬 Marcar como En revisión (pending)")
def mark_pending(modeladmin, request, queryset):
    updated = queryset.update(status=SupportTicket.STATUS_PENDING)
    modeladmin.message_user(request, f"{updated} ticket(s) marcado(s) como En revisión.")


@admin.action(description="✅ Marcar como Resuelto")
def mark_resolved(modeladmin, request, queryset):
    from django.utils import timezone as _tz
    updated = queryset.update(
        status=SupportTicket.STATUS_RESOLVED,
        resolved_at=_tz.now(),
    )
    modeladmin.message_user(request, f"{updated} ticket(s) marcado(s) como Resuelto.")


@admin.action(description="🔒 Marcar como Cerrado")
def mark_closed(modeladmin, request, queryset):
    updated = queryset.update(status=SupportTicket.STATUS_CLOSED)
    modeladmin.message_user(request, f"{updated} ticket(s) marcado(s) como Cerrado.")


# ─────────────────────────────────────────────
# Compliance Center — O.2f-1
#
# Read-only observation surface only, mirroring the Treasury Audit &
# Reconciliation block (O.2e-1). No path here creates, edits or deletes
# an EmailVerification / TermsAcceptance / TOTPDevice row, and nothing
# here touches broker_audit.py / audit.py / two_factor.py /
# email_verification.py or any gate (_is_email_verified,
# _has_accepted_terms, the 2FA/KYC withdrawal gates) — those stay exactly
# as they are, in views.py.
#
# TOTPDevice.secret is deliberately absent from `fields` itself (not just
# `readonly_fields`) — Django never renders a field that isn't listed,
# so it cannot appear on the change form under any circumstance, not
# even as a masked/read-only value.
#
# KYCProfile is untouched in this block — still registered exactly as
# it was, still in CORE OPERATIONS. Its approve_kyc/reject_kyc actions
# are not modified.
# ─────────────────────────────────────────────

@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    list_display   = ("user", "verified", "verified_at")
    list_filter    = ("verified",)
    search_fields  = ("user__username", "user__email")
    fields         = ("user", "verified", "verified_at")
    readonly_fields = fields
    ordering       = ("-verified_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TermsAcceptance)
class TermsAcceptanceAdmin(admin.ModelAdmin):
    list_display   = ("user", "terms_version", "risk_disclaimer_version", "accepted_at", "ip_address")
    list_filter    = ("terms_version", "risk_disclaimer_version", "accepted_at")
    search_fields  = ("user__username", "user__email", "ip_address")
    fields         = (
        "user", "terms_version", "risk_disclaimer_version",
        "accepted_at", "ip_address", "user_agent",
    )
    readonly_fields = fields
    ordering       = ("-accepted_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TOTPDevice)
class TOTPDeviceAdmin(admin.ModelAdmin):
    # "secret" is intentionally excluded from `fields` — see module note above.
    list_display   = ("user", "confirmed", "created_at", "confirmed_at")
    list_filter    = ("confirmed",)
    search_fields  = ("user__username", "user__email")
    fields         = ("user", "confirmed", "created_at", "confirmed_at")
    readonly_fields = fields
    ordering       = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):

    _STATUS_COLORS = {
        SupportTicket.STATUS_OPEN:     ("#0a1a2a", "#7986cb"),
        SupportTicket.STATUS_PENDING:  ("#2a1a00", "#f1c40f"),
        SupportTicket.STATUS_RESOLVED: ("#0a2a1a", "#27ae60"),
        SupportTicket.STATUS_CLOSED:   ("#1a1a1a", "#888888"),
    }
    _PRIORITY_COLORS = {
        SupportTicket.PRIORITY_LOW:    ("#1a1a1a", "#888888"),
        SupportTicket.PRIORITY_NORMAL: ("#0a1a2a", "#7986cb"),
        SupportTicket.PRIORITY_HIGH:   ("#2a1500", "#e67e22"),
        SupportTicket.PRIORITY_URGENT: ("#2a0000", "#e74c3c"),
    }

    @admin.display(description="Status")
    def status_badge(self, obj):
        bg, fg = self._STATUS_COLORS.get(obj.status, ("#1a1a1a", "#aaa"))
        return _badge(obj.get_status_display(), bg, fg)

    @admin.display(description="Priority")
    def priority_badge(self, obj):
        bg, fg = self._PRIORITY_COLORS.get(obj.priority, ("#1a1a1a", "#aaa"))
        return _badge(obj.get_priority_display(), bg, fg)

    @admin.display(description="User")
    def user_col(self, obj):
        return format_html(
            '<strong>{}</strong><br><small style="color:#888">{}</small>',
            obj.user.username, obj.user.email,
        )

    list_display   = ("id", "user_col", "category", "subject", "status_badge", "priority_badge", "created_at", "updated_at")
    list_filter    = ("status", "priority", "category")
    search_fields  = ("user__email", "user__username", "subject", "message")
    ordering       = ("-created_at",)
    date_hierarchy = "created_at"
    actions        = [mark_pending, mark_resolved, mark_closed]

    readonly_fields = ("user", "category", "subject", "message", "created_at", "updated_at", "resolved_at")

    fieldsets = (
        ("Ticket", {
            "fields": ("user", "category", "subject", "message"),
        }),
        ("Estado", {
            "fields": ("status", "priority", "admin_note", "resolved_at"),
        }),
        ("Fechas", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


# ─────────────────────────────────────────────
# Funded payout — H.2/H.3 admin actions
# (logic lives in simulator/funded_payouts.py)
# ─────────────────────────────────────────────

@admin.register(FundedPayoutRequest)
class FundedPayoutRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "funded_type", "status_badge", "trader_cut",
        "cycle_profit", "created_at", "reviewed_by",
    )
    list_filter   = ("status", "funded_type")
    search_fields = ("user__username", "user__email")
    ordering      = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = (
        "enrollment", "funded_account", "funded_config", "user",
        "cycle_profit", "trader_cut", "broker_cut", "profit_split_pct",
        "balance_snapshot", "initial_balance_snapshot", "funded_type",
        "ledger_entry", "wallet_credit_tx", "withdrawal_request",
        "cycle_reset_at", "reviewed_by", "reviewed_at",
        "created_at", "updated_at",
    )
    actions = ["admin_approve_sim_payout", "admin_approve_internal_payout"]

    _STATUS_COLORS_FPR = {
        "pending":    ("#2a1a00", "#ffa726"),
        "approved":   ("#0a2a1a", "#26a69a"),
        "processing": ("#0a1a2a", "#29b6f6"),
        "completed":  ("#0a2a0a", "#66bb6a"),
        "rejected":   ("#2a0000", "#ef5350"),
        "failed":     ("#2a0000", "#e74c3c"),
        "cancelled":  ("#1a1a1a", "#888888"),
    }

    @admin.display(description="Status")
    def status_badge(self, obj):
        bg, fg = self._STATUS_COLORS_FPR.get(obj.status, ("#1a1a1a", "#aaa"))
        return _badge(obj.get_status_display(), bg, fg)

    @admin.action(description="Approve FUNDED_SIM payout (H.2)")
    @superuser_required_action
    def admin_approve_sim_payout(self, request, queryset):
        ok = err = 0
        for fpr in queryset:
            try:
                approve_sim_payout(fpr, request.user)
                ok += 1
            except FundedPayoutAlreadyProcessed as exc:
                self.message_user(request, str(exc), messages.WARNING)
                err += 1
            except InsufficientFundedBalance as exc:
                self.message_user(request, str(exc), messages.ERROR)
                err += 1
            except ValueError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                err += 1
        if ok:
            self.message_user(
                request,
                f"{ok} FUNDED_SIM payout(s) approved successfully.",
                messages.SUCCESS,
            )

    @admin.action(description="Approve FUNDED_INTERNAL payout + submit to NowPayments (H.3)")
    @superuser_required_action
    def admin_approve_internal_payout(self, request, queryset):
        from django.urls import reverse as _rev
        ok = err = 0
        callback_url = request.build_absolute_uri(
            _rev("simulator:withdraw_payout_callback")
        )
        for fpr in queryset:
            try:
                approve_internal_payout(fpr, request.user, callback_url)
                ok += 1
            except FundedPayoutAlreadyProcessed as exc:
                self.message_user(request, str(exc), messages.WARNING)
                err += 1
            except InsufficientFundedBalance as exc:
                self.message_user(request, str(exc), messages.ERROR)
                err += 1
            except ValueError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                err += 1
            except Exception as exc:
                self.message_user(
                    request,
                    f"NowPayments error on FPR #{fpr.pk}: {exc}",
                    messages.ERROR,
                )
                err += 1
        if ok:
            self.message_user(
                request,
                f"{ok} FUNDED_INTERNAL payout(s) approved and submitted to NowPayments.",
                messages.SUCCESS,
            )


# ─────────────────────────────────────────────
# Branding
# ─────────────────────────────────────────────

admin.site.site_header = "Money Broker — Risk Desk"
admin.site.site_title = "Money Broker"
admin.site.index_title = "Risk & Dealing Administration"


# ─────────────────────────────────────────────────────────────────────────────
# O.4b-2 — Treasury Permission Assignment Hardening (closes CRIT-2)
#
# TreasuryHardenedUserAdmin replaces Django's stock UserAdmin with one
# additional, independent restriction: the four Treasury permissions
# (can_submit/review/execute/recover_treasury_*) and is_superuser can
# only be granted/revoked by a superuser, and even a superuser cannot
# change either of those two things on THEIR OWN account through this
# form (self-grant prevention, O.4b Fase 0 decision 4). Every other
# field — including every non-Treasury permission — keeps its normal
# Django behavior for whoever already has auth.change_user, unchanged.
#
# Both protections are server-side, not cosmetic: user_permissions is
# restricted via formfield_for_manytomany()'s queryset (Django's
# ModelMultipleChoiceField.clean() rejects any submitted Treasury
# permission id that isn't in that queryset — a raw POST injection
# fails validation, not just a hidden checkbox), and is_superuser is
# marked disabled=True on the form for non-superusers (Django's
# ModelForm ignores submitted values for a disabled field and keeps
# the instance's current value — this is enforced in form.clean(), not
# in a template).
# ─────────────────────────────────────────────────────────────────────────────

from .treasury_permissions import (
    TREASURY_PERMISSION_CODENAMES,
    held_treasury_codenames as _held_treasury_codenames,
    record_concentration_blocked as _record_concentration_blocked,
    treasury_permission_queryset as _treasury_permission_queryset,
    would_be_concentrated as _would_be_concentrated,
)


def _audit_admin_treasury_permission_change(*, action, actor, target, permission,
                                             force_used: bool = False):
    """
    O.4b-2 — writes exactly one AuditLog row + one BrokerAuditEvent for a
    REAL grant or revoke performed through TreasuryHardenedUserAdmin.
    Reuses the exact event constants introduced in O.4b-1
    (EV_TREASURY_PERMISSION_GRANTED/REVOKED) — via="django_admin" is the
    only thing that distinguishes this call site from the management-
    command one. Never raises: an audit failure here must not surface as
    a form-save failure, since the permission mutation has already
    committed by the time this runs (called from save_related(), after
    super().save_related() has returned).

    action: "granted" | "revoked".

    O.4b-3 — also records the resulting Treasury permission combination
    and whether it is concentrated, and the current value of
    TREASURY_ROLE_CONCENTRATION_BLOCKING. force_used is always False
    here — TreasuryHardenedUserAdmin has no override mechanism (O.4b-3
    Fase 0: acceptable for this block that --force exists only in the
    management command; Django Admin always blocks when the flag is on)
    — the parameter exists only so this function's signature mirrors
    the CLI one and so a future admin-side override, if ever built,
    doesn't need a second audit writer.
    """
    from django.conf import settings

    from simulator.management.commands.assign_treasury_role import TREASURY_ROLE_CODENAMES

    role = next(
        (r for r, codename in TREASURY_ROLE_CODENAMES.items() if codename == permission.codename),
        permission.codename,
    )
    resulting_codenames = _held_treasury_codenames(target)
    resulting_count = len(resulting_codenames)

    detail = {
        "target_user_id": target.pk,
        "target_username": target.username,
        "role": role,
        "codename": permission.codename,
        "granted_by": actor.pk,
        "via": "django_admin",
        "is_self_grant": target.pk == actor.pk,
        "resulting_treasury_permission_count": resulting_count,
        "treasury_permissions": list(resulting_codenames),
        "concentration_detected": resulting_count > 1,
        "blocking_enabled": getattr(settings, "TREASURY_ROLE_CONCENTRATION_BLOCKING", False),
        "force_used": force_used,
        "actor": actor.pk,
        "outcome": action,
    }
    event_type = (
        "treasury.permission_granted" if action == "granted" else "treasury.permission_revoked"
    )

    try:
        from .models import AuditLog
        AuditLog.objects.create(
            event_type=event_type,
            action=f"Treasury permission '{permission.codename}' ({role}) {action} for user "
                   f"'{target.username}' via Django Admin (actor: {actor.username})",
            user=actor,
            detail=detail,
        )
    except Exception:
        pass

    from . import broker_audit as _audit
    _audit.record_admin_event(
        event_type=event_type,
        severity=_audit.Severity.WARNING,
        description=f"Treasury permission '{permission.codename}' ({role}) {action} for user "
                    f"#{target.pk} via Django Admin (actor: #{actor.pk})",
        actor_id=actor.pk,
        source_module="simulator.admin.TreasuryHardenedUserAdmin",
        metadata=detail,
    )


from django.contrib.auth.admin import UserAdmin as _DjangoUserAdmin


class TreasuryHardenedUserAdmin(_DjangoUserAdmin):
    """
    Subclasses Django's own auth.UserAdmin directly — every existing
    field/fieldset/behavior (add form, password change view,
    filter_horizontal, list_display, etc.) stays 100% intact except for
    the three overrides below.
    """

    def get_form(self, request, obj=None, **kwargs):
        # Stash the target user on the request so formfield_for_
        # manytomany() below (which Django calls with `request` only,
        # never `obj`) can tell whether this is a self-edit. Read, not
        # persisted anywhere — a plain request-scoped attribute, gone
        # once the response is built.
        request._o4b2_target_user = obj
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser and "is_superuser" in form.base_fields:
            # Server-side: Django's ModelForm ignores POSTed values for
            # a disabled field and keeps the instance's current value —
            # enforced in BoundField/Form.clean(), not a template
            # attribute. Applies identically whether the non-superuser
            # is editing themselves or anyone else (O.4b-2 requirement 5).
            form.base_fields["is_superuser"].disabled = True
        return form

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "user_permissions" and field is not None:
            target = getattr(request, "_o4b2_target_user", None)
            is_self_edit = target is not None and target.pk == request.user.pk
            if not request.user.is_superuser or is_self_edit:
                # Excluded from the queryset entirely — not hidden by
                # CSS/JS. A raw POST containing one of these permission
                # ids fails ModelMultipleChoiceField.clean() (the id is
                # not a member of this restricted queryset), so the
                # whole field is rejected as invalid rather than
                # silently accepted.
                field.queryset = field.queryset.exclude(
                    pk__in=_treasury_permission_queryset().values_list("pk", flat=True),
                )
        return field

    def save_related(self, request, form, formsets, change):
        target = form.instance
        treasury_pks = set(_treasury_permission_queryset().values_list("pk", flat=True))

        before_ids = (
            set(target.user_permissions.filter(pk__in=treasury_pks).values_list("pk", flat=True))
            if change else set()
        )

        super().save_related(request, form, formsets, change)

        is_self_edit = change and target.pk == request.user.pk
        restricted = (not request.user.is_superuser) or is_self_edit

        if restricted:
            # This actor/target combination never had the four Treasury
            # permissions in the form's queryset at all (see
            # formfield_for_manytomany above), so save_m2m() above can
            # only have DROPPED any pre-existing ones (a `.set()` call
            # replaces the whole relation with what was submitted) —
            # never added new ones. Restore the exact pre-save state:
            # no grant, no revoke is possible through this path,
            # regardless of what was submitted.
            if before_ids:
                target.user_permissions.add(*before_ids)
            return

        # Unrestricted path: a superuser editing someone else. Treasury
        # permissions WERE in the form's queryset and may have
        # genuinely changed — diff before/after and audit exactly what
        # changed, once per permission (O.4b-2 requirement 7).
        by_pk = {p.pk: p for p in _treasury_permission_queryset()}
        after_ids = set(
            target.user_permissions.filter(pk__in=treasury_pks).values_list("pk", flat=True),
        )
        added_pks = after_ids - before_ids
        removed_pks = before_ids - after_ids

        # O.4b-3 — Treasury Role Concentration Guard. super().save_
        # related() above has already persisted whatever was submitted
        # (Django's normal .set() semantics) — if blocking is enabled
        # and the resulting combination would be concentrated because
        # of a genuinely NEW addition, revert exactly the newly-added
        # permissions here (never an already-held one, never a revoke),
        # so the persisted end state matches what the CLI achieves by
        # never calling .add() in the first place. No admin-side
        # override exists (O.4b-3 Fase 0: acceptable for this block) —
        # this always blocks when the flag is on, unconditionally.
        from django.conf import settings

        blocking_enabled = getattr(settings, "TREASURY_ROLE_CONCENTRATION_BLOCKING", False)
        if added_pks and blocking_enabled:
            before_codenames = {by_pk[pk].codename for pk in before_ids}
            added_codenames = {by_pk[pk].codename for pk in added_pks}
            if len(before_codenames | added_codenames) > 1:
                target.user_permissions.remove(*added_pks)
                for codename in sorted(added_codenames):
                    _record_concentration_blocked(
                        actor_id=request.user.pk, target=target, codename=codename,
                        current_codenames=tuple(sorted(before_codenames)), via="django_admin",
                    )
                self.message_user(
                    request,
                    "⚠ Treasury role concentration blocked — granting "
                    f"{', '.join(sorted(added_codenames))} to '{target.username}' would "
                    f"result in {len(before_codenames | added_codenames)} Treasury "
                    "permissions. No Treasury permission changes were applied for this user.",
                    level=messages.WARNING,
                )
                added_pks = set()  # nothing net added — do not audit these as granted

        if not added_pks and not removed_pks:
            return  # no-op (or fully blocked above) — nothing further to audit

        for pk in added_pks:
            _audit_admin_treasury_permission_change(
                action="granted", actor=request.user, target=target, permission=by_pk[pk],
            )
        for pk in removed_pks:
            _audit_admin_treasury_permission_change(
                action="revoked", actor=request.user, target=target, permission=by_pk[pk],
            )


# ─────────────────────────────────────────────────────────────────────────────
# Admin sidebar reorganization — ADMIN_UI.1
# Groups all simulator models into named sections without touching registrations,
# URLs, migrations or any business logic.  Fully reversible: delete this block
# and admin.site.__class__ = MoneyBrokerAdminSite to undo.
# ─────────────────────────────────────────────────────────────────────────────

class MoneyBrokerAdminSite(admin.AdminSite):
    """
    Override get_app_list to reorganize the sidebar into logical sections.
    All models remain registered on admin.site — only the visual grouping changes.
    """

    _SECTIONS = [
        ("CORE OPERATIONS", "core_ops", [
            "kycprofile",
            "supportticket",
            "auditlog",
        ]),
        ("COMPLIANCE", "compliance", [
            "emailverification",
            "termsacceptance",
            "totpdevice",
        ]),
        ("TRADING ENGINE", "trading_eng", [
            "tradingaccount",
            "trade",
            "position",
            "riskrule",
            "tradingviolation",
            "drawdownsnapshot",
            "traderscore",
        ]),
        ("FUNDING PROGRAMS", "funding", [
            "challengeproduct",
            "challengeenrollment",
            "fundedconfig",
            "fundedpayoutrequest",
            "accountproduct",
        ]),
        ("PAYMENTS & LEDGER", "payments", [
            "deposit",
            "withdrawalrequest",
            "ledgerentry",
            "purchase",
            "bonus",
        ]),
        ("BROKER OPERATIONS", "broker_ops", [
            "routingdecision",
            "liquidityprovider",
            "liquiditydecision",
            "liquidityledger",
            "dealingdeskdecision",
            "brokerauditevent",
            "brokerspreadconfig",
            "instrument",
        ]),
        ("TREASURY", "treasury", [
            "wallet",
            "wallettransaction",
            "internaltransfer",
            "treasuryoperationrequest",  # O.2g-1b — was falling into UNCATEGORIZED
        ]),
        ("BROKER BUSINESS", "broker_biz", [
            "brokerledger",
            "brokersnapshot",
            "brokerrevenuesnapshot",
            "brokerdocument",
        ]),
        ("GROWTH", "growth", [
            "referral",
        ]),
        ("TOOLS", "tools", [
            "expertadvisor",
            "calendarevent",
        ]),
    ]

    def get_app_list(self, request, app_label=None):
        original = super().get_app_list(request, app_label)

        # Separate auth/contenttypes apps from simulator models
        sim_models: dict = {}
        other_apps: list = []
        for app in original:
            if app["app_label"] == "simulator":
                for m in app["models"]:
                    sim_models[m["object_name"].lower()] = m
            else:
                other_apps.append(app)

        # Build custom sections
        custom = list(other_apps)
        placed: set = set()
        for section_name, section_label, model_keys in self._SECTIONS:
            section_models = []
            for key in model_keys:
                if key in sim_models:
                    section_models.append(sim_models[key])
                    placed.add(key)
            if section_models:
                custom.append({
                    "name": section_name,
                    "app_label": section_label,
                    "app_url": "",
                    "has_module_perms": True,
                    "models": section_models,
                })

        # Safety net — any simulator model not listed above goes to UNCATEGORIZED
        leftover = [m for k, m in sim_models.items() if k not in placed]
        if leftover:
            custom.append({
                "name": "UNCATEGORIZED",
                "app_label": "uncategorized",
                "app_url": "",
                "has_module_perms": True,
                "models": leftover,
            })

        return custom

    @staticmethod
    def _admin_login_rate_limit_keys(request):
        """
        O.4d-1 — derive the two independent rate-limit dimensions for an
        admin login attempt. Username is normalized (stripped + lowercased)
        for the Redis key ONLY — this never touches actual authentication,
        which remains 100% Django's own case-sensitive behavior, untouched.
        Truncated to the same max length already used for
        username_attempted in the existing failure audit event, so a
        maliciously long "username" can't be used to build unbounded key
        strings.
        """
        from . import broker_audit as _audit
        from .observability import get_client_ip

        ip = get_client_ip(request)
        username_raw = (request.POST.get("username", "") or "").strip()
        username_norm = username_raw.lower()[:_audit.ADMIN_LOGIN_USERNAME_ATTEMPTED_MAX_LENGTH]
        ip_key = f"admin_login_fail:ip:{ip}"
        user_key = f"admin_login_fail:user:{username_norm}" if username_norm else None
        return ip, ip_key, username_norm, user_key

    def _render_admin_login_blocked(self, request):
        """
        O.4d-1 — the HTTP 429 response for a pre-check-blocked admin login
        attempt. Renders the REAL admin login template via 100% public
        Django APIs — no authentication is attempted, no real credentials
        are bound to any form:

          - AdminAuthenticationForm(request) is constructed UNBOUND (no
            data= kwarg) — Django never validates an unbound form, so
            .clean()/authenticate() are never reached. Its only purpose
            here is to render the empty username/password fields exactly
            like a fresh GET would.
          - messages.error() + the admin templates' own pre-existing
            {% block messages %} (admin/base.html, never overridden by
            login.html/base_site.html in this project) surfaces the
            generic notice — no manual form.add_error()/cleaned_data
            manipulation needed.
          - Context mirrors what AdminSite.login()/LoginView.get_context_
            data() build for a normal GET, using only public methods
            (self.each_context()) and constants (REDIRECT_FIELD_NAME).

        The message is deliberately generic: it never reveals whether the
        submitted username exists, whether the password was correct, any
        permission information, or anything about TOTP device state.
        """
        from django.contrib import messages as django_messages
        from django.contrib.admin.forms import AdminAuthenticationForm
        from django.contrib.auth import REDIRECT_FIELD_NAME
        from django.urls import reverse as _reverse

        django_messages.error(
            request,
            "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.",
        )
        context = {
            **self.each_context(request),
            "title": "Log in",
            "subtitle": None,
            "app_path": request.get_full_path(),
            "username": request.user.get_username(),
            REDIRECT_FIELD_NAME: (
                request.POST.get(REDIRECT_FIELD_NAME)
                or request.GET.get(REDIRECT_FIELD_NAME)
                or _reverse("admin:index", current_app=self.name)
            ),
            "form": AdminAuthenticationForm(request),
        }
        return render(request, self.login_template or "admin/login.html", context, status=429)

    def login(self, request, extra_context=None):
        """
        AUDIT-04b — observes the outcome of Django's own AdminSite.login()
        (which internally delegates to django.contrib.auth.views.LoginView
        + AdminAuthenticationForm). Never touches the form, the template,
        session handling, or the redirect — super() runs first and fully
        owns all of that; this method only reads request.user afterward.

        AdminAuthenticationForm.confirm_login_allowed() already rejects
        non-staff users before auth_login() is ever called, so "valid
        credentials but not staff" and "invalid credentials" both simply
        fall into the failed branch below — no separate reason code, by
        design (see AUDIT-04b design doc).

        O.4d-1 — Admin/TOTP Anti-Brute-Force Hardening (closes HIGH-3).
        Two independent, non-incrementing PRE-CHECKS (rate_peek(), never
        rate_check()) run before super().login() is ever called on a
        POST: if either the IP or the (normalized) attempted username is
        already at or over its threshold, super().login() is skipped
        entirely — no authentication attempt happens at all for an
        already-blocked identity — and a 429 is returned via the real
        admin login template (_render_admin_login_blocked() above).

        Only on a REAL, completed authentication failure (after
        super().login() has already run and request.user is still
        anonymous) do the counters increment, via rate_check() — a
        successful login NEVER touches these counters, so it can never
        consume the failure budget. This ordering is deliberate and is
        the entire reason this method does not simply call rate_check()
        upfront: rate_check() always increments, which would be wrong
        for a pre-check (see O.4d-1 Fase 0 finding).
        """
        from . import broker_audit as _audit
        from .observability import get_client_ip
        from .ratelimit import rate_check, rate_peek

        if request.method == "POST":
            ip, ip_key, username_norm, user_key = self._admin_login_rate_limit_keys(request)
            ip_blocked = rate_peek(ip_key) >= _audit.ADMIN_LOGIN_RATE_LIMIT_IP_THRESHOLD
            user_blocked = bool(user_key) and (
                rate_peek(user_key) >= _audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_THRESHOLD
            )
            if ip_blocked or user_blocked:
                return self._render_admin_login_blocked(request)

        response = super().login(request, extra_context)
        if request.method == "POST":
            if request.user.is_authenticated:
                _audit.record_auth_event(
                    event_type=_audit.EV_ADMIN_SITE_LOGIN_SUCCESS,
                    severity=_audit.Severity.WARNING,
                    actor_type=_audit.ActorType.STAFF,
                    user=request.user,
                    source_module="simulator.admin",
                    description=f"Admin site login succeeded for user #{request.user.pk}",
                    metadata={"ip": get_client_ip(request)},
                )
            else:
                username_attempted = (request.POST.get("username", "") or "").strip()
                username_attempted = username_attempted[:_audit.ADMIN_LOGIN_USERNAME_ATTEMPTED_MAX_LENGTH]
                _audit.record_auth_event(
                    event_type=_audit.EV_ADMIN_SITE_LOGIN_FAILED,
                    severity=_audit.Severity.WARNING,
                    actor_type=_audit.ActorType.STAFF,
                    source_module="simulator.admin",
                    description="Admin site login failed",
                    metadata={"username_attempted": username_attempted, "ip": get_client_ip(request)},
                )

                # O.4d-1 — increment ONLY now that we know this attempt
                # genuinely failed. A successful login never reaches this
                # branch, so it never consumes the failure budget.
                ip, ip_key, username_norm, user_key = self._admin_login_rate_limit_keys(request)
                _, ip_count = rate_check(
                    ip_key,
                    limit=_audit.ADMIN_LOGIN_RATE_LIMIT_IP_THRESHOLD,
                    window=_audit.ADMIN_LOGIN_RATE_LIMIT_IP_WINDOW_SECONDS,
                )
                if ip_count == _audit.ADMIN_LOGIN_RATE_LIMIT_IP_THRESHOLD:
                    _audit.record_auth_event(
                        event_type=_audit.EV_AUTH_RATE_LIMITED,
                        severity=_audit.Severity.WARNING,
                        actor_type=_audit.ActorType.STAFF,
                        source_module="simulator.admin",
                        description="Admin login rate limit reached by IP",
                        metadata={
                            "surface": "admin_login",
                            "dimension": "ip",
                            "ip": ip,
                            "attempt_count": ip_count,
                            "threshold": _audit.ADMIN_LOGIN_RATE_LIMIT_IP_THRESHOLD,
                            "window_seconds": _audit.ADMIN_LOGIN_RATE_LIMIT_IP_WINDOW_SECONDS,
                        },
                    )
                if user_key:
                    _, user_count = rate_check(
                        user_key,
                        limit=_audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_THRESHOLD,
                        window=_audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_WINDOW_SECONDS,
                    )
                    if user_count == _audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_THRESHOLD:
                        _audit.record_auth_event(
                            event_type=_audit.EV_AUTH_RATE_LIMITED,
                            severity=_audit.Severity.WARNING,
                            actor_type=_audit.ActorType.STAFF,
                            source_module="simulator.admin",
                            description="Admin login rate limit reached by username",
                            metadata={
                                "surface": "admin_login",
                                "dimension": "user",
                                "username_attempted": username_norm,
                                "attempt_count": user_count,
                                "threshold": _audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_THRESHOLD,
                                "window_seconds": _audit.ADMIN_LOGIN_RATE_LIMIT_USERNAME_WINDOW_SECONDS,
                            },
                        )
        return response

    def admin_view(self, view, cacheable=False):
        """
        O.4a-2 — wraps Django's own AdminSite.admin_view() (called via
        super() below, unmodified — this method never reimplements or
        bypasses its has_permission()/never_cache/csrf_protect behavior)
        with one additional, independent check: when
        TOTP_ADMIN_TREASURY_REQUIRED=True, any authenticated staff user
        for whom treasury_2fa_required() is True (superusers and holders
        of any of the four Treasury permissions — O.4a Fase 0 §3) must
        have a verified TOTP session (totp_session_verified()) before
        reaching the wrapped view.

        admin_view() is called via self.admin_site.admin_view(...) by
        every single URL registered under /admin/ — Django's own CRUD
        views AND every custom Treasury view (approve/reject/execute/
        recover/cancel/dashboard, all registered in this file's
        get_urls() methods) — so overriding it here is the one place
        that protects all of them without touching any of those view
        functions individually (O.4a Fase 0 §6/§11/§12: server-side,
        not button-hiding; direct URL access is equally protected).

        Deliberately NOT an override of has_permission(): that method is
        also consulted by AdminSite.login() to decide whether an
        already-authenticated user gets redirected straight to the index
        page. Folding the TOTP check into has_permission() would make
        that redirect decision False for a password-valid-but-not-yet-
        2FA-verified user, and Django's login() has no TOTP-awareness of
        its own — the user would be shown the login form again with
        nowhere to complete the TOTP step, an infinite loop. See O.4a
        Fase 0 design (rejected alternative). login() itself is not
        modified by this method.

        With TOTP_ADMIN_TREASURY_REQUIRED=False (the default), this
        method is a pure passthrough to super().admin_view() — the
        existing admin behavior is unchanged, byte-for-byte, for every
        caller (O.4a Fase 0 requirement 2).
        """
        original = super().admin_view(view, cacheable)

        def gated(request, *args, **kwargs):
            from django.conf import settings

            if getattr(settings, "TOTP_ADMIN_TREASURY_REQUIRED", False):
                user = request.user
                if getattr(user, "is_authenticated", False) and user.is_staff:
                    # admin:logout must stay reachable even mid-gate —
                    # the same exemption Django's own admin_view()
                    # already grants it against has_permission(), for
                    # the identical reason (never trap a user with no
                    # way out).
                    logout_path = reverse("admin:logout", current_app=self.name)
                    if request.path != logout_path:
                        from .two_factor import (
                            totp_session_verified, treasury_2fa_required,
                        )
                        if treasury_2fa_required(user) and not totp_session_verified(request):
                            from .models import TOTPDevice
                            request.session["2fa_next"] = request.get_full_path()
                            has_confirmed_device = TOTPDevice.objects.filter(
                                user=user, confirmed=True,
                            ).exists()
                            if has_confirmed_device:
                                return redirect("simulator:totp_verify")
                            return redirect("simulator:totp_setup")

            return original(request, *args, **kwargs)

        return gated


# Swap the class on the existing admin.site instance.
# All @admin.register() decorators already bound to this object remain intact.
# URLs remain unchanged. No registrations are affected.
admin.site.__class__ = MoneyBrokerAdminSite


# O.4b-2 — replace Django's default auth.User registration with the
# hardened one. django.contrib.auth.apps.AuthConfig.ready() registers
# the stock UserAdmin via autodiscovery before this module finishes
# importing, so it is unregistered here and re-registered with
# TreasuryHardenedUserAdmin — same model, same admin.site, only the
# ModelAdmin class differs.
from django.contrib.auth.models import User as _AuthUser

admin.site.unregister(_AuthUser)
admin.site.register(_AuthUser, TreasuryHardenedUserAdmin)
