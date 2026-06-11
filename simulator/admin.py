# simulator/admin.py
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum, Count, Q
from django.urls import path, reverse
from django.shortcuts import redirect, render
from django.utils.timezone import now
from django.utils.html import format_html

from .models import (
    TradingAccount, Position, Trade, LedgerEntry,
    Purchase, Deposit, WithdrawalRequest,
    RiskRule, DrawdownSnapshot, TradingViolation, TraderScore,
    BrokerSnapshot, SymbolExposure, TraderClassExposure,
    AuditLog,
    CalendarEvent, Referral, Bonus, BrokerDocument, ExpertAdvisor,
    BrokerLedger, BrokerSpreadConfig,
    BrokerEquitySnapshot, BrokerRevenueSnapshot,
    ChallengeProduct, ChallengeEnrollment, FundedConfig,
    KYCProfile, SupportTicket,
)
from . import challenge_engine


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
            # Add form: nothing is read-only (computed fields aren't shown at all)
            return ("created_at",)
        return self.readonly_fields

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
                from django.db import transaction as db_tx
                symbol = (request.POST.get("symbol") or "").strip()
                try:
                    px = float(request.POST.get("price")) if request.POST.get("price") else None
                except Exception:
                    px = None
                total_closed, total_pnl = 0, 0.0
                with db_tx.atomic():
                    qs = Position.objects.select_for_update().filter(account=account)
                    if symbol:
                        qs = qs.filter(symbol=symbol)
                    positions = list(qs)
                    for pos in positions:
                        exit_px = px if px is not None else float(pos.avg_price)
                        # PnL: BUY profits when exit > entry, SELL profits when exit < entry
                        pnl = ((exit_px - float(pos.avg_price)) * float(pos.qty)
                               if pos.side == "BUY"
                               else (float(pos.avg_price) - exit_px) * float(pos.qty))
                        Trade.objects.create(
                            account=account, symbol=pos.symbol,
                            trade_type=pos.side,          # record original side, consistent with WS consumer
                            lot_size=pos.qty, entry_price=pos.avg_price,
                            exit_price=Decimal(str(exit_px)), stop_loss=pos.sl,
                            take_profit=pos.tp, profit_loss=Decimal(str(pnl)),
                            opened_at=pos.opened_at, closed_at=now(),
                        )
                        bal_after = float(account.balance or 0) + pnl
                        LedgerEntry.objects.create(
                            account=account, event_type=LedgerEntry.EV_REALIZED,
                            amount=Decimal(str(pnl)), balance_after=Decimal(str(bal_after)),
                            meta={"reason": "admin_force_close", "symbol": pos.symbol},
                        )
                        account.balance = Decimal(str(bal_after))
                        account.equity  = Decimal(str(bal_after))
                        account.save(update_fields=["balance", "equity"])
                        pos.delete()
                        total_closed += 1
                        total_pnl += pnl
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

    list_display = ("id", "account", "event_type", "amount_colored", "balance_after", "created_at")
    list_filter = ("event_type", "created_at", "account")
    search_fields = ("account__user__username", "account__user__email", "event_type")
    readonly_fields = ("created_at", "balance_after")


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

    list_display = ("id", "user", "amount_usd", "crypto_currency", "status_badge", "created_at", "confirmed_at")
    list_filter = ("status", "crypto_currency", "created_at")
    search_fields = ("user__username", "user__email")
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

    pending = queryset.filter(status=WithdrawalRequest.STATUS_PENDING)
    if not pending.exists():
        modeladmin.message_user(request, "No hay retiros pendientes seleccionados.", messages.WARNING)
        return

    ok, errs = 0, []
    for wr in pending:
        try:
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
                reviewed_by      = request.user,
                reviewed_at      = now(),
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
            errs.append(f"#{wr.id}: {exc}")

    if ok:
        modeladmin.message_user(request, f"{ok} retiro(s) aprobados y enviados.", messages.SUCCESS)
    for e in errs:
        modeladmin.message_user(request, f"Error — {e}", messages.ERROR)


@admin.action(description="❌ Rechazar — devolver fondos al wallet")
def reject_withdrawals(modeladmin, request, queryset):
    from .wallet_ledger import credit_wallet, get_or_create_wallet
    from .models import WalletTransaction
    from django.core.mail import send_mail
    from django.conf import settings as _cfg

    pending = queryset.filter(status=WithdrawalRequest.STATUS_PENDING)
    if not pending.exists():
        modeladmin.message_user(request, "No hay retiros pendientes seleccionados.", messages.WARNING)
        return

    count = 0
    for wr in pending:
        try:
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
    list_display   = ('symbol', 'spread_pips', 'enabled', 'is_dynamic', 'min_spread', 'max_spread', 'created_at')
    list_filter    = ('enabled', 'is_dynamic')
    search_fields  = ('symbol',)
    list_editable  = ('spread_pips', 'enabled')
    readonly_fields = ('created_at',)
    ordering       = ('symbol',)


@admin.register(BrokerLedger)
class BrokerLedgerAdmin(admin.ModelAdmin):
    list_display   = ('id', 'revenue_type', 'amount', 'source_account', 'symbol', 'created_at')
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

        qs = BrokerLedger.objects.all()
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
        _base     = BrokerLedger.objects.all()
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
            revenue_choices = BrokerLedger.REVENUE_CHOICES,
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

    now          = timezone.now()
    today_start  = now.replace(hour=0, minute=0, second=0, microsecond=0)
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
    bl = BrokerLedger.objects
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

    # Broker PnL decomposition (B-book counter-party model)
    unrealized_risk = float(bk_snap.broker_pnl_unrealized) if bk_snap else 0.0
    net_broker_pnl  = t_lifetime + unrealized_risk

    # Daily pace — extrapolate today's revenue to EOD
    elapsed_s = max(1, (now - today_start).total_seconds())
    pace_eod  = round(t_today * (86400 / elapsed_s), 2) if t_today > 0 else 0.0

    snap_age_s = int((now - rev_snap.taken_at).total_seconds()) if rev_snap else -1

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
            "realized":        t_lifetime,
            "unrealized_risk": unrealized_risk,
            "net":             net_broker_pnl,
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

    list_display    = ("taken_at", "total_col", "period_col",
                       "active_accounts", "open_positions", "exposure_col")
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
        bl = BrokerLedger.objects
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
    list_display  = ("name", "tier", "price_usd", "account_size", "phases_summary", "profit_split_pct", "is_active", "created_at")
    list_filter   = ("is_active", "tier")
    search_fields = ("name",)
    readonly_fields = ("created_at",)

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
    )

    @admin.display(description="Phases")
    def phases_summary(self, obj):
        return format_html(
            "P1: {}% / P2: {}%",
            obj.p1_profit_target_pct,
            obj.p2_profit_target_pct,
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
        from django.utils import timezone
        updated = queryset.filter(status=KYCProfile.STATUS_PENDING).update(
            status=KYCProfile.STATUS_APPROVED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
            rejection_reason="",
        )
        self.message_user(request, f"{updated} KYC profile(s) approved.")

    @admin.action(description="Reject selected KYC profiles")
    def reject_kyc(self, request, queryset):
        from django.utils import timezone
        updated = queryset.filter(status=KYCProfile.STATUS_PENDING).update(
            status=KYCProfile.STATUS_REJECTED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(request, f"{updated} KYC profile(s) rejected.")

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
# Branding
# ─────────────────────────────────────────────

admin.site.site_header = "Money Brokers — Risk Desk"
admin.site.site_title = "Money Brokers"
admin.site.index_title = "Risk & Dealing Administration"
