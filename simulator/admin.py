# simulator/admin.py
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum, Q
from django.urls import path, reverse
from django.shortcuts import redirect, render
from django.utils.timezone import now
from django.utils.html import format_html

from .models import TradingAccount, Position, Trade, LedgerEntry, Purchase, Deposit


# ==========================
# Inlines (en la vista de la cuenta)
# ==========================
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
        "entry_price", "exit_price",
        "stop_loss", "take_profit",
        "profit_loss", "opened_at", "closed_at",
    )
    readonly_fields = ("opened_at", "closed_at")
    show_change_link = True


# ==========================
# Actions útiles (TradingAccount)
# ==========================
@admin.action(description="Resetear balance a balance inicial del tier")
def reset_balance(modeladmin, request, queryset):
    tier_defaults = {"10K": 10000, "50K": 50000, "100K": 100000}
    updated = 0
    for acc in queryset:
        base = tier_defaults.get(getattr(acc, "tier", "10K"), 10000)
        acc.balance = base
        acc.equity = base
        acc.save(update_fields=["balance", "equity"])
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


@admin.action(description="Desactivar NETTING → HEDGING (múltiples posiciones)")
def disable_netting(modeladmin, request, queryset):
    rows = queryset.update(netting_mode=False)
    modeladmin.message_user(request, f"{rows} cuenta(s) en modo HEDGING.")


# ==========================
# ModelAdmins
# ==========================
@admin.register(TradingAccount)
class TradingAccountAdmin(admin.ModelAdmin):
    def dealing_link(self, obj):
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[obj.pk])
        return format_html('<a class="button" href="{}">Desk</a>', url)
    dealing_link.short_description = "Desk"

    list_display = (
        "id", "user", "tier", "phase", "currency", "leverage", "netting_mode",
        "balance", "equity", "drawdown", "profit_target", "max_drawdown",
        "status", "created_at", "dealing_link",   # ← botón al final de la fila
    )
    list_filter = ("tier", "phase", "status", "netting_mode", "currency", "created_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    inlines = [PositionInline, TradeInline]
    actions = [reset_balance, suspend_accounts, activate_accounts, enable_netting, disable_netting]

    fieldsets = (
        ("Propietario y plan", {"fields": ("user", "tier", "phase", "status")}),
        ("Parámetros de la cuenta", {"fields": ("currency", "leverage", "netting_mode")}),
        ("Saldos y límites", {
            "fields": ("balance", "equity", "drawdown", "profit_target", "max_drawdown")
        }),
        ("Metadatos", {"fields": ("created_at",)}),
    )

    # ----- Dealing Desk -----
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
        account = TradingAccount.objects.filter(pk=account_id).first()
        if not account:
            messages.error(request, "Cuenta no encontrada.")
            return redirect("admin:simulator_tradingaccount_changelist")

        # Cierre forzado por símbolo (POST)
        if request.method == "POST" and request.POST.get("action") == "force_close":
            symbol = (request.POST.get("symbol") or "").strip()
            try:
                px = float(request.POST.get("price")) if request.POST.get("price") else None
            except Exception:
                px = None

            qs = Position.objects.filter(account=account)
            if symbol:
                qs = qs.filter(symbol=symbol)

            total_closed = 0
            total_pnl = 0.0

            for pos in qs:
                exit_px = px if px is not None else pos.avg_price
                pnl = (exit_px - pos.avg_price) * pos.qty if pos.side == "BUY" else (pos.avg_price - exit_px) * pos.qty

                Trade.objects.create(
                    account=account,
                    symbol=pos.symbol,
                    trade_type="SELL" if pos.side == "BUY" else "BUY",
                    lot_size=pos.qty,
                    entry_price=pos.avg_price,
                    exit_price=exit_px,
                    stop_loss=pos.sl,
                    take_profit=pos.tp,
                    profit_loss=pnl,
                    opened_at=pos.opened_at,
                    closed_at=now(),
                )

                bal_after = (account.balance or 0) + pnl
                LedgerEntry.objects.create(
                    account=account,
                    event_type="FORCED_CLOSE",
                    amount=pnl,
                    balance_after=bal_after,
                )

                account.balance = bal_after
                account.equity = bal_after
                account.save(update_fields=["balance", "equity"])

                pos.delete()
                total_closed += 1
                total_pnl += pnl

            if total_closed:
                messages.success(request, f"Cerradas {total_closed} posición(es). PnL total: {total_pnl:.2f}")
            else:
                messages.info(request, "No hay posiciones para cerrar con ese criterio.")

            return redirect("admin:simulator_tradingaccount_dealing_desk", account_id=account.id)

        # Exposición por símbolo (BUY/SELL en mayúsculas)
        agg = (
            Position.objects.filter(account=account)
            .values("symbol")
            .annotate(
                long_qty=Sum("qty", filter=Q(side="BUY")),
                short_qty=Sum("qty", filter=Q(side="SELL")),
            )
            .order_by("symbol")
        )
        exposure = []
        for r in agg:
            l = float(r["long_qty"] or 0)
            s = float(r["short_qty"] or 0)
            exposure.append({
                "symbol": r["symbol"],
                "long_qty": l,
                "short_qty": s,
                "net_qty": l - s,
            })

        context = dict(
            self.admin_site.each_context(request),
            title=f"Dealing Desk — Cuenta #{account.id}",
            account=account,
            exposure=exposure,
        )
        return render(request, "admin/dealing_desk_inline.html", context)


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "symbol", "side", "qty", "avg_price", "sl", "tp", "external_id", "opened_at")
    list_filter = ("side", "symbol", "opened_at", "account")
    search_fields = ("symbol", "account__user__username", "account__user__email", "external_id")
    readonly_fields = ("opened_at",)
    list_editable = ("sl", "tp")


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = (
        "id", "account", "symbol", "trade_type", "lot_size",
        "entry_price", "exit_price",
        "stop_loss", "take_profit", "profit_loss",
        "opened_at", "closed_at",
    )
    list_filter = ("trade_type", "symbol", "opened_at", "closed_at", "account")
    search_fields = ("account__user__username", "account__user__email", "symbol")
    readonly_fields = ("opened_at", "closed_at")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "event_type", "amount", "balance_after", "created_at")
    list_filter = ("event_type", "created_at", "account")
    search_fields = ("account__user__username", "account__user__email", "event_type")
    readonly_fields = ("created_at", "balance_after")


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tier", "code", "used", "created_at")
    list_filter = ("tier", "used", "created_at")
    search_fields = ("user__username", "user__email", "code")
    readonly_fields = ("created_at",)


@admin.register(Deposit)
class DepositAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "amount_usd", "crypto_currency", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at", "confirmed_at", "nowpayments_payment_id", "nowpayments_invoice_url")


# ==========================
# Branding del Admin
# ==========================
admin.site.site_header = "Money Brokers — Admin"
admin.site.site_title = "Money Brokers"
admin.site.index_title = "Panel de administración"