# simulator/broker_risk.py
"""
RISK-02 — Broker Risk Limits Engine (institutional foundation).

Answers exactly one question, independent of routing/book decisions:

    Can the broker accept this NEW exposure?

If NO, the caller must reject the order BEFORE it executes. This module
does not decide A-Book/B-Book (that is BOOK-04's job, which will consume
this engine's PASS/FAIL decision) and does not touch execution, margin,
spread, commission, leverage, or stopout — it is a new, additive gate
that runs alongside those, never replacing any of them.

── FASE 1 audit — what already validates orders today ──────────────────
Every existing pre-trade check is PER-ACCOUNT scope; none aggregates
exposure across the whole broker book:

  - consumers.py::_compute_pretrade_margin_guard /
    _compute_atomic_open_guard — this account's symbol whitelist, product
    lot cap, per-trade margin %, total margin %, margin-level projection.
  - risk_engine.py::validate_order_risk — this account's RiskRule
    (max_lot_size, max_open_positions, max_daily_loss_pct,
    max_drawdown_pct), account-status gate.
  - risk_engine.py::evaluate_position_risk — this account's exposure_pct/
    margin risk_level (LOW/MEDIUM/HIGH/EXTREME) for the risk-confirm UX.

None of these can answer "is EURUSD broker-wide already near its lot
cap across ALL accounts?" — that concept does not exist anywhere in the
codebase before this module. RiskRule (models.py) is a real, separate,
per-account model; RISK-02 does not duplicate or replace it — it adds
the missing BROKER-WIDE layer on top, reusing RISK-01's
broker_exposure.py as its read of current state (never re-deriving
exposure with a new formula).

── FASE 3 — limits (all configurable via Django settings, generous
    defaults so this gate never fires in normal test/demo traffic
    unless a caller explicitly lowers a limit — see each constant) ────
No persisted config model is created (FASE 3: "no crear migraciones
enormes" when constants suffice). Every limit is a plain module constant,
overridable per-deployment via the matching Django setting name with no
migration required.

── Post-review corrections ──────────────────────────────────────────────
1. ATOMICITY — evaluating these rules and creating the Position used to
   be two separate steps (a pre-lock RISK-02 check in consumers.py,
   followed later by _db_open_position_atomic). That had a genuine TOCTOU
   race: two concurrent opens on DIFFERENT accounts (so TradingAccount's
   own lock can't serialize them) could each read the same broker-wide
   exposure, both evaluate PASS, and jointly exceed a broker-wide limit.
   Fixed by moving the ENTIRE evaluate-then-open sequence inside
   _db_open_position_atomic's single transaction, behind a new
   select_for_update() singleton lock (BrokerRiskLock, models.py),
   acquired FIRST in the global lock order: BrokerRiskLock ->
   TradingAccount -> Position. See that model's docstring and the
   module-level LOCK ORDER note in consumers.py for the full design.
   validate_new_order() itself remains a pure read+decide function with
   no opinion on locking — the CALLER is responsible for calling it from
   inside the right lock, which today means exactly one call site.
2. FAIL-CLOSED PRICING — MAX_GROSS_NOTIONAL/MAX_NET_NOTIONAL used to
   return a WARNING (order still allowed) when price data was
   unavailable. That let an understated notional produce a false PASS.
   Now: missing price/contract_size for the new order, OR
   pricing_coverage_pct < 100% anywhere in the broker book (RISK-01),
   makes those two checks FAIL and the overall decision's reason_code
   REASON_PRICING_INCOMPLETE ("RISK_PRICING_INCOMPLETE") — allowed
   defaults to False whenever monetary limits can't be verified. Purely
   quantitative (lot/count) rules are unaffected.
3. MAX_OPEN_POSITIONS renamed to MAX_OPEN_POSITIONS_BROKER_WIDE
   everywhere (constant, rule identifier, messages) to remove any
   ambiguity with RiskRule.max_open_positions (per-account, unrelated,
   untouched).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from . import broker_exposure as _exposure

log = logging.getLogger("simulator.broker_risk")

_ZERO = Decimal("0")


def _setting(name: str, default):
    return getattr(settings, name, default)


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — the 9 required limits. Lot/quantity limits are in lots (same
# unit Position.qty already uses); notional limits are in account-currency
# USD-equivalent (same unit broker_exposure.py's *_notional fields use).
# Defaults are deliberately generous — large enough that no existing
# test/demo scenario in this codebase ever hits them by accident; an
# operator tightens them via Django settings, not by editing this file.
# ─────────────────────────────────────────────────────────────────────────
MAX_SYMBOL_EXPOSURE_LOTS       = _setting("RISK02_MAX_SYMBOL_EXPOSURE_LOTS", Decimal("100000"))
MAX_ACCOUNT_EXPOSURE_LOTS      = _setting("RISK02_MAX_ACCOUNT_EXPOSURE_LOTS", Decimal("100000"))
MAX_TOTAL_BROKER_EXPOSURE_LOTS = _setting("RISK02_MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("1000000"))
MAX_LONG_EXPOSURE_LOTS         = _setting("RISK02_MAX_LONG_EXPOSURE_LOTS", Decimal("1000000"))
MAX_SHORT_EXPOSURE_LOTS        = _setting("RISK02_MAX_SHORT_EXPOSURE_LOTS", Decimal("1000000"))
MAX_GROSS_NOTIONAL             = _setting("RISK02_MAX_GROSS_NOTIONAL", Decimal("1000000000000"))
MAX_NET_NOTIONAL               = _setting("RISK02_MAX_NET_NOTIONAL", Decimal("1000000000000"))
MAX_POSITION_SIZE_LOTS         = _setting("RISK02_MAX_POSITION_SIZE_LOTS", Decimal("100000"))
MAX_OPEN_POSITIONS_BROKER_WIDE = _setting("RISK02_MAX_OPEN_POSITIONS_BROKER_WIDE", 1_000_000)

RULE_NAMES = (
    "MAX_SYMBOL_EXPOSURE", "MAX_ACCOUNT_EXPOSURE", "MAX_TOTAL_BROKER_EXPOSURE",
    "MAX_LONG_EXPOSURE", "MAX_SHORT_EXPOSURE", "MAX_GROSS_NOTIONAL",
    "MAX_NET_NOTIONAL", "MAX_POSITION_SIZE", "MAX_OPEN_POSITIONS_BROKER_WIDE",
)

# Correction 3 — MAX_OPEN_POSITIONS_BROKER_WIDE is a count of open
# Position rows across EVERY account in the broker, not a per-account
# limit. It does NOT duplicate or replace RiskRule.max_open_positions
# (models.py), which is a separate, per-account, pre-existing field
# unrelated to this module. If a future block wants a per-account
# broker-facing count limit, it needs its own distinctly-named rule —
# this one is deliberately, permanently broker-wide.
MAX_OPEN_POSITIONS_RULE = "MAX_OPEN_POSITIONS_BROKER_WIDE"

# Correction 2 — the reason_code surfaced when a notional-dependent rule
# (MAX_GROSS_NOTIONAL/MAX_NET_NOTIONAL) cannot be safely evaluated because
# either the new order's own price is missing, or ANY open position
# broker-wide lacks a fresh price (RISK-01's pricing_coverage_pct < 100%).
# Fail-closed: allowed=False whenever this applies, never a PASS built on
# an understated notional. Purely quantitative rules (lot/count-based) are
# unaffected and still evaluated normally.
REASON_PRICING_INCOMPLETE = "RISK_PRICING_INCOMPLETE"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARNING = "WARNING"


# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — decision contract
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class RiskCheckResult:
    rule: str
    status: str                 # PASS / FAIL / WARNING
    message: str
    current_value: "Decimal | int | None" = None
    requested_value: "Decimal | int | None" = None
    limit_value: "Decimal | int | None" = None


@dataclass
class RiskLimitDecision:
    allowed: bool
    reason_code: "str | None"           # None when allowed
    reason_message: str
    risk_checks: list = field(default_factory=list)   # list[RiskCheckResult], ALL rules evaluated, not just the failing one
    blocked_limit: "str | None" = None  # the rule name that caused FAIL, if any
    current_value: "Decimal | int | None" = None
    requested_value: "Decimal | int | None" = None
    limit_value: "Decimal | int | None" = None
    margin_after: "Decimal | None" = None
    exposure_after: "Decimal | None" = None
    generated_at: "datetime | None" = None


def _check(rule, status, message, current=None, requested=None, limit=None) -> RiskCheckResult:
    return RiskCheckResult(rule=rule, status=status, message=message,
                            current_value=current, requested_value=requested, limit_value=limit)


# ─────────────────────────────────────────────────────────────────────────
# FASE 4 — individual validators. Each returns the list of RiskCheckResult
# it evaluated (never just the first failure) so the caller/report always
# sees the full picture, matching FASE 2's "risk_checks" contract.
# ─────────────────────────────────────────────────────────────────────────
def validate_symbol_limit(symbol: str, requested_qty: Decimal) -> list:
    """MAX_SYMBOL_EXPOSURE — broker-wide gross lots on one symbol."""
    current = _exposure.broker_exposure_for_symbol(symbol).gross_quantity
    projected = current + requested_qty
    if projected > MAX_SYMBOL_EXPOSURE_LOTS:
        return [_check(
            "MAX_SYMBOL_EXPOSURE", STATUS_FAIL,
            f"{symbol}: exposición actual {current} + nueva {requested_qty} = {projected} "
            f"lotes supera el límite del broker ({MAX_SYMBOL_EXPOSURE_LOTS} lotes).",
            current=current, requested=requested_qty, limit=MAX_SYMBOL_EXPOSURE_LOTS,
        )]
    return [_check(
        "MAX_SYMBOL_EXPOSURE", STATUS_PASS,
        f"{symbol}: {projected}/{MAX_SYMBOL_EXPOSURE_LOTS} lotes tras la nueva orden.",
        current=current, requested=requested_qty, limit=MAX_SYMBOL_EXPOSURE_LOTS,
    )]


def validate_account_limit(account_id: int, requested_qty: Decimal) -> list:
    """MAX_ACCOUNT_EXPOSURE — broker-wide gross lots for one account
    (across every symbol that account holds)."""
    current = _exposure.broker_exposure_for_account(account_id).gross_quantity
    projected = current + requested_qty
    if projected > MAX_ACCOUNT_EXPOSURE_LOTS:
        return [_check(
            "MAX_ACCOUNT_EXPOSURE", STATUS_FAIL,
            f"Cuenta #{account_id}: exposición actual {current} + nueva {requested_qty} = "
            f"{projected} lotes supera el límite de cuenta ({MAX_ACCOUNT_EXPOSURE_LOTS} lotes).",
            current=current, requested=requested_qty, limit=MAX_ACCOUNT_EXPOSURE_LOTS,
        )]
    return [_check(
        "MAX_ACCOUNT_EXPOSURE", STATUS_PASS,
        f"Cuenta #{account_id}: {projected}/{MAX_ACCOUNT_EXPOSURE_LOTS} lotes tras la nueva orden.",
        current=current, requested=requested_qty, limit=MAX_ACCOUNT_EXPOSURE_LOTS,
    )]


def validate_total_limit(side: str, requested_qty: Decimal, book, *, price=None, contract_size=None) -> list:
    """MAX_TOTAL_BROKER_EXPOSURE, MAX_LONG_EXPOSURE, MAX_SHORT_EXPOSURE,
    MAX_GROSS_NOTIONAL, MAX_NET_NOTIONAL — whole-book, all symbols/accounts.

    `book` is a pre-fetched BrokerExposureBreakdown (broker_exposure_snapshot())
    — the caller (validate_new_order) fetches it once and shares it with
    validate_position_limit too, avoiding a redundant broker-wide query.

    Correction 2 (fail-closed pricing): MAX_GROSS_NOTIONAL/MAX_NET_NOTIONAL
    require every dollar figure involved to be trustworthy. They FAIL
    (never WARNING, never a silent PASS) whenever either:
      - this order's own price/contract_size were not supplied, or
      - book.pricing_coverage_pct < 100% (ANY open position broker-wide,
        on any symbol, lacks a fresh price — see RISK-01's
        broker_exposure.py) — an understated gross/net notional could
        otherwise hide a real limit breach behind a false PASS.
    Quantity-based checks (total/long/short — lots, not dollars) never
    need a price and always run normally regardless.
    """
    results = []

    total_current = book.gross_quantity
    total_projected = total_current + requested_qty
    if total_projected > MAX_TOTAL_BROKER_EXPOSURE_LOTS:
        results.append(_check(
            "MAX_TOTAL_BROKER_EXPOSURE", STATUS_FAIL,
            f"Exposición total del broker {total_current} + {requested_qty} = {total_projected} "
            f"lotes supera el límite ({MAX_TOTAL_BROKER_EXPOSURE_LOTS} lotes).",
            current=total_current, requested=requested_qty, limit=MAX_TOTAL_BROKER_EXPOSURE_LOTS,
        ))
    else:
        results.append(_check(
            "MAX_TOTAL_BROKER_EXPOSURE", STATUS_PASS,
            f"Exposición total del broker: {total_projected}/{MAX_TOTAL_BROKER_EXPOSURE_LOTS} lotes.",
            current=total_current, requested=requested_qty, limit=MAX_TOTAL_BROKER_EXPOSURE_LOTS,
        ))

    is_buy = str(side).upper() == "BUY"
    long_current = book.long_quantity
    short_current = book.short_quantity
    long_projected = long_current + (requested_qty if is_buy else _ZERO)
    short_projected = short_current + (requested_qty if not is_buy else _ZERO)

    if long_projected > MAX_LONG_EXPOSURE_LOTS:
        results.append(_check(
            "MAX_LONG_EXPOSURE", STATUS_FAIL,
            f"Exposición LONG del broker {long_current} + {requested_qty if is_buy else 0} = "
            f"{long_projected} lotes supera el límite ({MAX_LONG_EXPOSURE_LOTS} lotes).",
            current=long_current, requested=requested_qty, limit=MAX_LONG_EXPOSURE_LOTS,
        ))
    else:
        results.append(_check(
            "MAX_LONG_EXPOSURE", STATUS_PASS,
            f"Exposición LONG del broker: {long_projected}/{MAX_LONG_EXPOSURE_LOTS} lotes.",
            current=long_current, requested=requested_qty, limit=MAX_LONG_EXPOSURE_LOTS,
        ))

    if short_projected > MAX_SHORT_EXPOSURE_LOTS:
        results.append(_check(
            "MAX_SHORT_EXPOSURE", STATUS_FAIL,
            f"Exposición SHORT del broker {short_current} + {requested_qty if not is_buy else 0} = "
            f"{short_projected} lotes supera el límite ({MAX_SHORT_EXPOSURE_LOTS} lotes).",
            current=short_current, requested=requested_qty, limit=MAX_SHORT_EXPOSURE_LOTS,
        ))
    else:
        results.append(_check(
            "MAX_SHORT_EXPOSURE", STATUS_PASS,
            f"Exposición SHORT del broker: {short_projected}/{MAX_SHORT_EXPOSURE_LOTS} lotes.",
            current=short_current, requested=requested_qty, limit=MAX_SHORT_EXPOSURE_LOTS,
        ))

    pricing_incomplete = (
        price is None or contract_size is None
        or book.pricing_coverage_pct < Decimal("100")
    )
    if pricing_incomplete:
        reasons = []
        if price is None or contract_size is None:
            reasons.append("precio/contract_size de la nueva orden no proporcionado")
        if book.pricing_coverage_pct < Decimal("100"):
            reasons.append(
                f"cobertura de precios del broker {book.pricing_coverage_pct}% "
                f"({book.unpriced_position_count} posición(es) sin precio fresco: "
                f"{', '.join(book.stale_or_missing_symbols) or '—'})"
            )
        msg = (
            "Fail-closed: no se puede verificar el notional con seguridad — "
            + "; ".join(reasons) + ". gross_notional/net_notional podrían estar "
            "subestimados; la orden se rechaza en lugar de asumir que los límites "
            "monetarios se cumplen."
        )
        results.append(_check(
            "MAX_GROSS_NOTIONAL", STATUS_FAIL, msg,
            current=book.gross_notional, limit=MAX_GROSS_NOTIONAL,
        ))
        results.append(_check(
            "MAX_NET_NOTIONAL", STATUS_FAIL, msg,
            current=book.net_notional, limit=MAX_NET_NOTIONAL,
        ))
        return results

    price_d = price if isinstance(price, Decimal) else Decimal(str(price))
    cs_d = contract_size if isinstance(contract_size, Decimal) else Decimal(str(contract_size))
    requested_notional = requested_qty * price_d * cs_d

    gross_current = book.gross_notional
    gross_projected = gross_current + requested_notional
    if gross_projected > MAX_GROSS_NOTIONAL:
        results.append(_check(
            "MAX_GROSS_NOTIONAL", STATUS_FAIL,
            f"Notional bruto del broker {gross_current} + {requested_notional} = {gross_projected} "
            f"supera el límite ({MAX_GROSS_NOTIONAL}).",
            current=gross_current, requested=requested_notional, limit=MAX_GROSS_NOTIONAL,
        ))
    else:
        results.append(_check(
            "MAX_GROSS_NOTIONAL", STATUS_PASS,
            f"Notional bruto del broker: {gross_projected}/{MAX_GROSS_NOTIONAL}.",
            current=gross_current, requested=requested_notional, limit=MAX_GROSS_NOTIONAL,
        ))

    net_current = book.net_notional
    signed_requested_notional = requested_notional if is_buy else -requested_notional
    net_projected = net_current + signed_requested_notional
    if abs(net_projected) > MAX_NET_NOTIONAL:
        results.append(_check(
            "MAX_NET_NOTIONAL", STATUS_FAIL,
            f"Notional neto del broker {net_current} + {signed_requested_notional} = {net_projected} "
            f"supera el límite ({MAX_NET_NOTIONAL}).",
            current=net_current, requested=signed_requested_notional, limit=MAX_NET_NOTIONAL,
        ))
    else:
        results.append(_check(
            "MAX_NET_NOTIONAL", STATUS_PASS,
            f"Notional neto del broker: {net_projected}/{MAX_NET_NOTIONAL}.",
            current=net_current, requested=signed_requested_notional, limit=MAX_NET_NOTIONAL,
        ))

    return results


def validate_position_limit(requested_qty: Decimal, book) -> list:
    """MAX_POSITION_SIZE (single order, no current-state lookup needed) +
    MAX_OPEN_POSITIONS_BROKER_WIDE (open Position COUNT across every
    account in the broker — see RULE_NAMES' comment for why this is
    deliberately broker-wide, not per-account). The count check
    conservatively assumes the new order becomes a brand-new Position row
    (+1) even though a netting-mode merge would not actually increase the
    count — a safe over-estimate for a risk gate, never an under-count.

    `book` is the same pre-fetched BrokerExposureBreakdown validate_total_limit
    uses — shared, not re-queried."""
    results = []

    if requested_qty > MAX_POSITION_SIZE_LOTS:
        results.append(_check(
            "MAX_POSITION_SIZE", STATUS_FAIL,
            f"Tamaño de orden {requested_qty} lotes supera el máximo por orden "
            f"({MAX_POSITION_SIZE_LOTS} lotes).",
            requested=requested_qty, limit=MAX_POSITION_SIZE_LOTS,
        ))
    else:
        results.append(_check(
            "MAX_POSITION_SIZE", STATUS_PASS,
            f"Tamaño de orden: {requested_qty}/{MAX_POSITION_SIZE_LOTS} lotes.",
            requested=requested_qty, limit=MAX_POSITION_SIZE_LOTS,
        ))

    current_count = book.open_position_count
    projected_count = current_count + 1
    if projected_count > MAX_OPEN_POSITIONS_BROKER_WIDE:
        results.append(_check(
            MAX_OPEN_POSITIONS_RULE, STATUS_FAIL,
            f"Posiciones abiertas del broker (TODAS las cuentas) {current_count} + 1 = "
            f"{projected_count} supera el límite ({MAX_OPEN_POSITIONS_BROKER_WIDE}).",
            current=current_count, requested=1, limit=MAX_OPEN_POSITIONS_BROKER_WIDE,
        ))
    else:
        results.append(_check(
            MAX_OPEN_POSITIONS_RULE, STATUS_PASS,
            f"Posiciones abiertas del broker (TODAS las cuentas): "
            f"{projected_count}/{MAX_OPEN_POSITIONS_BROKER_WIDE}.",
            current=current_count, requested=1, limit=MAX_OPEN_POSITIONS_BROKER_WIDE,
        ))

    return results


# ─────────────────────────────────────────────────────────────────────────
# BOOK-06g — Controlled Activation Foundation (2026-07-27). BOOK-06h.1
# scoped the exclusion query to the canary allowlist; BOOK-06h.2 wired
# _resolve_broker_exposure_for_validation() into validate_new_order()
# (below) as its sole functional change. Flag OFF and empty allowlist
# are both still the default — see settings.py.
# ─────────────────────────────────────────────────────────────────────────
def _should_use_dealing_desk_adjusted_exposure(account_id: int) -> bool:
    """
    Gate — never decides HOW the adjusted exposure is computed, only
    WHETHER it should be attempted at all for this account_id. Lives
    here, not in broker_exposure.py/broker_risk_shadow.py, same
    caller-owns-the-gate precedent already used by
    TradingConsumer._should_activate_routing_decision() (BOOK-04f).

    Deliberately the opposite default from BOOK-04f's own allowlist
    semantics: an EMPTY DEALING_DESK_EXPOSURE_ACCOUNT_IDS means NO
    account qualifies, even with the master flag True — never "no
    restriction". BOOK-04f's allowlists gate a per-order decision, where
    "empty means unrestricted" preserves 100%-of-orders behavior for an
    environment that already had the master flag on. This gate controls
    an AGGREGATE, whole-book calculation instead — "no restriction"
    here would mean "every account's is_simulated_hedge=True positions
    are excluded from the broker-wide aggregate", the opposite of the
    minimal, controlled first activation this block exists to support.

    Never raises — any unexpected failure here is treated as "no",
    same contract as _should_activate_routing_decision().
    """
    try:
        if not getattr(settings, "DEALING_DESK_EXPOSURE_ENABLED", False):
            return False

        allowlist = getattr(settings, "DEALING_DESK_EXPOSURE_ACCOUNT_IDS", frozenset())
        return account_id in allowlist
    except Exception:
        return False


def _resolve_broker_exposure_for_validation(account_id: int):
    """
    The single decision point for whether validate_new_order() (once a
    future block wires this in — not done here) should evaluate limits
    against the official broker-wide exposure or the Dealing-Desk-
    adjusted one. Not called by validate_new_order() today — this
    function exists, fully tested in isolation, with zero real callers,
    exactly as authorized for BOOK-06g.

    Fail-SAFE, not fail-open in the observational sense: any failure
    here falls back to the official calculation already trusted today
    — it never means "skip the limit check". Never raises.

    BOOK-06h.1: the exclusion is scoped to
    DEALING_DESK_EXPOSURE_ACCOUNT_IDS — only positions belonging to
    canary-authorized accounts are ever excluded, never every
    is_simulated_hedge=True position broker-wide. Joined via
    routing_decision__account_id rather than position__account_id
    because RoutingDecision is append-only and never deleted, while
    Position is always deleted on close.
    """
    if not _should_use_dealing_desk_adjusted_exposure(account_id):
        return _exposure.broker_exposure_snapshot()

    try:
        from .models import DealingDeskDecision

        allowlist = getattr(settings, "DEALING_DESK_EXPOSURE_ACCOUNT_IDS", frozenset())
        excluded_ids = frozenset(
            DealingDeskDecision.objects
            .filter(
                is_simulated_hedge=True,
                routing_decision__account_id__in=allowlist,
            )
            .exclude(position_id__isnull=True)
            .values_list("position_id", flat=True)
        )
        adjusted = _exposure.calculate_broker_exposure(exclude_position_ids=excluded_ids)

        # BOOK-06h.3 — observability only (closes RC-1 Finding F-05:
        # "no se puede confirmar desde logs con qué frecuencia se usó
        # el canario"). Never feeds back into the risk decision — the
        # object returned below is the exact same `adjusted` value
        # validate_new_order() already used before this block existed.
        #
        # One extra read-only call to the SAME official formula,
        # exclusively on this success path (never on flag OFF/outside
        # the allowlist/empty allowlist — see the early return above),
        # mirrors broker_risk_shadow.py's own "call the formula twice
        # rather than duplicate it" precedent instead of re-deriving
        # gross/net notional independently.
        #
        # The comparison numbers are stashed as a plain, transient
        # attribute on the returned dataclass instance — never a new
        # dataclass field, never module/thread-local state (unsafe
        # under concurrent async requests) — purely so
        # validate_new_order() can emit ONE combined structured log
        # once risk_allowed/reason_code are known, several calls later
        # in the same sequence. If this observability block itself
        # fails, it is swallowed here and `adjusted` is returned
        # exactly as it would have been anyway — validate_new_order()
        # simply has nothing to log for this call, same as the
        # official path.
        try:
            official = _exposure.calculate_broker_exposure()
            adjusted._dealing_desk_observability = {
                "excluded_positions_count": official.open_position_count - adjusted.open_position_count,
                "excluded_notional": official.gross_notional - adjusted.gross_notional,
                "official_gross_notional": official.gross_notional,
                "adjusted_gross_notional": adjusted.gross_notional,
                "official_net_notional": official.net_notional,
                "adjusted_net_notional": adjusted.net_notional,
            }
        except Exception as obs_exc:
            log.error(
                "[broker_risk] dealing desk exposure observability computation failed for account=%s: %r",
                account_id, obs_exc, exc_info=True,
            )

        return adjusted
    except Exception as exc:
        log.error(
            "[broker_risk] dealing desk exposure resolution failed for account=%s: %r",
            account_id, exc, exc_info=True,
        )
        return _exposure.broker_exposure_snapshot()


def _log_dealing_desk_exposure_usage(account_id, book, *, allowed, reason_code):
    """
    BOOK-06h.3 — closes RC-1 Finding F-05. Emits exactly one structured
    INFO log per validate_new_order() call, but ONLY when the adjusted
    book was genuinely used by _resolve_broker_exposure_for_validation()
    above — never on flag OFF, outside the allowlist, an empty
    allowlist, or a resolver fallback caused by an exception (in every
    one of those cases `book` carries no `_dealing_desk_observability`
    attribute at all, so this simply returns without logging — no
    noise, matching BOOK-06h.3's own requirement).

    Called from validate_new_order() after risk_allowed/reason_code are
    final — this function has no opinion on and never affects either
    value, purely observational. Never raises.
    """
    obs = getattr(book, "_dealing_desk_observability", None)
    if obs is None:
        return
    try:
        log.info(
            "[broker_risk] dealing_desk_exposure_used account_id=%s mode=adjusted "
            "excluded_positions_count=%s excluded_notional=%s "
            "official_gross_notional=%s adjusted_gross_notional=%s "
            "official_net_notional=%s adjusted_net_notional=%s "
            "risk_allowed=%s reason_code=%s",
            account_id, obs["excluded_positions_count"], obs["excluded_notional"],
            obs["official_gross_notional"], obs["adjusted_gross_notional"],
            obs["official_net_notional"], obs["adjusted_net_notional"],
            allowed, reason_code,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# FASE 4/5 — the single orchestrator every caller should use.
# ─────────────────────────────────────────────────────────────────────────
def validate_new_order(
    *,
    account_id: int,
    symbol: str,
    side: str,
    qty,
    price=None,
    contract_size=None,
) -> RiskLimitDecision:
    """
    Evaluate a prospective new order against every broker-wide risk limit
    (FASE 3's 9 rules) and return one RiskLimitDecision. Runs ALL checks
    (never short-circuits on the first FAIL) so risk_checks always shows
    the complete picture — allowed is False if ANY check is FAIL.

    This is the ONE function live order-open code should call (FASE 5 —
    "una sola integración"). It does not open, reject, or mutate anything
    itself — purely a read+decide function, same shape as
    broker_pnl.py/broker_exposure.py before it.
    """
    qty_d = qty if isinstance(qty, Decimal) else Decimal(str(qty))

    # Fetched ONCE, shared by validate_total_limit and validate_position_limit
    # (both used to call broker_exposure_snapshot() independently).
    #
    # BOOK-06h.2: resolved through _resolve_broker_exposure_for_validation(),
    # which returns broker_exposure_snapshot() unchanged unless account_id is
    # inside the DEALING_DESK_EXPOSURE_ACCOUNT_IDS canary (flag OFF by
    # default) — fail-safe to the same official snapshot on any error.
    book = _resolve_broker_exposure_for_validation(account_id)

    checks: list = []
    checks += validate_symbol_limit(symbol, qty_d)
    checks += validate_account_limit(account_id, qty_d)
    checks += validate_total_limit(side, qty_d, book, price=price, contract_size=contract_size)
    checks += validate_position_limit(qty_d, book)

    # Correction 2 — fail-closed pricing. Whenever the notional checks
    # above couldn't be trusted, the OVERALL decision's reason_code is the
    # explicit RISK_PRICING_INCOMPLETE marker — not the generic
    # MAX_GROSS_NOTIONAL/MAX_NET_NOTIONAL rule name — even though those
    # two individual checks are what's actually FAIL in risk_checks (kept
    # there, verbatim, for full traceability).
    pricing_incomplete = (
        price is None or contract_size is None
        or book.pricing_coverage_pct < Decimal("100")
    )

    # exposure_after — broker-wide projected gross lots post-order, always
    # available (no price needed). margin_after — this account's projected
    # margin usage (RISK-01's mirror of consumers.py::_margin_used_total's
    # real formula) + this order's own required margin; only computable
    # when price/contract_size are supplied, never fabricated otherwise.
    total_check = next(c for c in checks if c.rule == "MAX_TOTAL_BROKER_EXPOSURE")
    exposure_after = (total_check.current_value or _ZERO) + (total_check.requested_value or _ZERO)

    margin_after = None
    if price is not None and contract_size is not None:
        account_exposure = _exposure.broker_exposure_for_account(account_id)
        price_d = price if isinstance(price, Decimal) else Decimal(str(price))
        cs_d = contract_size if isinstance(contract_size, Decimal) else Decimal(str(contract_size))
        from .models import TradingAccount
        from market_data.symbol_specs import get_spec
        try:
            account = TradingAccount.objects.only("leverage").get(pk=account_id)
            spec = get_spec(symbol)
            lev = max(1, min(int(account.leverage or 50), int(spec.max_leverage)))
            new_order_margin = abs(price_d * qty_d * cs_d) / Decimal(lev)
            margin_after = account_exposure.margin_used + new_order_margin
        except (TradingAccount.DoesNotExist, KeyError):
            margin_after = None

    failures = [c for c in checks if c.status == STATUS_FAIL]
    allowed = len(failures) == 0

    if allowed:
        _log_dealing_desk_exposure_usage(account_id, book, allowed=True, reason_code=None)
        return RiskLimitDecision(
            allowed=True, reason_code=None, reason_message="Orden dentro de todos los límites del broker.",
            risk_checks=checks, margin_after=margin_after, exposure_after=exposure_after,
            generated_at=timezone.now(),
        )

    # Correction 2 — a pricing-incomplete failure gets the distinct,
    # explicit RISK_PRICING_INCOMPLETE reason_code, prioritized over any
    # other failure that may also be present (a caller/trader needs to
    # know "we couldn't verify this" is a different situation than "you
    # exceeded a limit").
    pricing_failure = next(
        (c for c in failures if pricing_incomplete and c.rule in ("MAX_GROSS_NOTIONAL", "MAX_NET_NOTIONAL")),
        None,
    )
    first = pricing_failure or failures[0]
    reason_code = REASON_PRICING_INCOMPLETE if pricing_failure is not None else first.rule

    _log_dealing_desk_exposure_usage(account_id, book, allowed=False, reason_code=reason_code)
    return RiskLimitDecision(
        allowed=False,
        reason_code=reason_code,
        reason_message=first.message,
        risk_checks=checks,
        blocked_limit=first.rule,
        current_value=first.current_value,
        requested_value=first.requested_value,
        limit_value=first.limit_value,
        margin_after=margin_after,
        exposure_after=exposure_after,
        generated_at=timezone.now(),
    )
