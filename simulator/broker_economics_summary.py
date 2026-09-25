"""
simulator/broker_economics_summary.py — BROKER-ECONOMICS-03 FASE B.

Read-only composition layer over the certified SSOTs:
  - broker_pnl.py (BOOK-03) — Gross Broker Economics. Called directly,
    never recomputed, never modified. gross_broker_economic_result below
    is literally BrokerPnLBreakdown.broker_net_pnl, verbatim.
  - IBCommissionObligation / IBCommissionAdjustment — IB economic
    expense, read-only, respecting the exact lifecycle certified in
    BROKER-ECONOMICS-03 FASE A: PENDING -> APPROVED -> CREDITED, or
    CANCELLED/REVERSED (terminal); is_held is orthogonal to status;
    IBCommissionAdjustment is the only correction mechanism and never
    mutates the original obligation's snapshot fields.
  - Deposit / WithdrawalRequest — Capital Flows, deliberately kept in a
    separate section, NEVER folded into revenue or gross/retained
    economics.

This module creates ZERO BrokerLedger rows, ZERO IBCommissionObligation
rows, ZERO wallet/treasury movements, and required NO migration to
build — every field it reads already existed. It does not change how
Money Broker earns money; it measures, aggregates, and exposes the
existing economy.

Coverage/completeness discipline (mirrors broker_pnl.py's own
coverage_pct pattern, generalized): every category carries an explicit
CategoryCoverage(status, note) —
  COMPLETE                              — every relevant row is captured
  PARTIAL                               — some rows/paths are known to
                                           be missing (cite which)
  NOT_CAPTURED                          — the concept has no data source
                                           in this codebase at all yet
  NOT_APPLICABLE                        — this dimension genuinely does
                                           not apply to this rule/event
  POLICY_APPROVED_IMPLEMENTATION_PENDING — an Owner-approved economic
                                           category with no production
                                           writer yet (challenge fee,
                                           withdrawal fee — see FASE B
                                           Decisions A/B)
A category with unknown/incomplete data NEVER silently reports as if it
were COMPLETE. An amount that is genuinely $0.00 today because no writer
exists is never reported as a bare absence a reader could mistake for
"confirmed zero forever."

Challenge fee revenue and withdrawal fee revenue (BROKER-ECONOMICS-04A
— Coverage Truth Correction): both categories now have certified,
committed, forward-only production writers —
challenge_revenue.py::record_challenge_fee_revenue() and
withdrawal_economics.py::record_withdrawal_fee_revenue(), each booking
exactly one DB-idempotent BrokerLedger row per certified economic event
(challenge enrollment after verified payment; WithdrawalRequest
reaching COMPLETED). This module itself remains strictly READ-ONLY —
it creates zero BrokerLedger rows, zero challenge/withdrawal rows,
regardless of which writers exist elsewhere. The withdrawal fee
percentage is read from WithdrawalFeeConfig by that writer, not
hardcoded anywhere in this module. Provider/network/payment
processing cost for either category remains a separate, unimplemented
concern — see challenge_coverage/withdrawal_fee_coverage below, and
note that captured revenue is never the same claim as captured profit
or margin.

The pending/stop/limit-trigger spread markup gap (BROKER-ECONOMICS-02A-0,
re-confirmed unchanged) is measured two ways: an exact, non-estimated
COUNT of trigger-path opens in the period (LotExecutionEvent.entry_path
== ENTRY_PENDING_TRIGGER — the durable, unconditional anchor this model
was built for), and a best-effort DOLLAR estimate using each symbol's
CURRENT BrokerSpreadConfig.spread_pips (a real, persisted, current
config value — never an invented number) through the unmodified,
certified spread_engine.calculate_spread_revenue() formula. This
estimate deliberately does NOT include any per-account markup override
or the dynamic multiplier chain (both require live, per-connection state
this module has no business resolving) — it is a conservative floor,
labeled as such. It is an ESTIMATE ONLY: never added to
gross_broker_economic_result, never booked as revenue.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from . import broker_pnl
from .broker_pnl import (  # noqa: F401 - re-exported for caller convenience
    PERIOD_CUSTOM, PERIOD_LAST_24H, PERIOD_LIFETIME, PERIOD_MONTH,
    PERIOD_TODAY, PERIOD_WEEK, utc_period_window,
)
from .models import (
    BrokerSpreadConfig, Deposit, IBCommissionAdjustment, IBCommissionObligation,
    IBCommissionRule, LotExecutionEvent, WithdrawalRequest,
)

_ZERO = Decimal("0.00")

# ── Coverage status vocabulary ──────────────────────────────────────────
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NOT_CAPTURED = "NOT_CAPTURED"
COVERAGE_NOT_APPLICABLE = "NOT_APPLICABLE"
COVERAGE_POLICY_APPROVED_PENDING = "POLICY_APPROVED_IMPLEMENTATION_PENDING"

VALID_COVERAGE_STATUSES = frozenset({
    COVERAGE_COMPLETE, COVERAGE_PARTIAL, COVERAGE_NOT_CAPTURED,
    COVERAGE_NOT_APPLICABLE, COVERAGE_POLICY_APPROVED_PENDING,
})

# Rule types whose source event carries a real, resolvable symbol/account
# (per BROKER-ECONOMICS-03 FASE A's per-rule-type dimension table).
_SYMBOL_ACCOUNT_ATTRIBUTABLE_RULE_TYPES = frozenset({
    IBCommissionRule.RULE_PER_LOT,
    IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
    IBCommissionRule.RULE_SPREAD_REVENUE_SHARE,
})
_NOT_ATTRIBUTABLE_KEY = "NOT_ATTRIBUTABLE"
_NOT_ATTRIBUTABLE_LABEL = "Not attributable (event carries no symbol/account)"


@dataclass
class CategoryCoverage:
    status: str
    note: str

    def __post_init__(self):
        if self.status not in VALID_COVERAGE_STATUSES:
            raise ValueError(f"invalid coverage status: {self.status!r}")


@dataclass
class IBExpenseSummary:
    """pending/approved/credited are gross sums of calculated_amount at
    that status. net_paid = credited - reversed_or_adjusted — the only
    field meant to answer "how much has the broker actually, netly,
    paid out." A PENDING or APPROVED obligation is never counted as
    paid expense."""
    pending: Decimal = _ZERO
    approved: Decimal = _ZERO
    credited: Decimal = _ZERO
    reversed_or_adjusted: Decimal = _ZERO
    net_paid: Decimal = _ZERO
    coverage: CategoryCoverage = None


@dataclass
class CapitalFlowsSummary:
    """Deliberately separate — never counted as revenue or folded into
    gross/retained broker economics."""
    deposits_total: Decimal = _ZERO
    withdrawals_total: Decimal = _ZERO
    coverage: CategoryCoverage = None


@dataclass
class RetainedEconomicsSummary:
    gross_broker_economic_result: Decimal = _ZERO
    net_paid_ib_expense: Decimal = _ZERO
    known_execution_liquidity_costs: Decimal = _ZERO
    known_payment_transaction_costs: Decimal = _ZERO
    other_captured_variable_costs: Decimal = _ZERO
    retained_economics: Decimal = _ZERO
    status: str = COVERAGE_PARTIAL
    coverage: CategoryCoverage = None


@dataclass
class PendingSpreadEstimate:
    """ESTIMATE ONLY. trigger_open_count is exact (a real row count, not
    an estimate). estimated_foregone_revenue is a conservative,
    current-config-based estimate, or None if config coverage was
    insufficient to compute one honestly — never a fabricated number."""
    trigger_open_count: int = 0
    estimated_foregone_revenue: "Decimal | None" = None
    coverage: CategoryCoverage = None


@dataclass
class BrokerEconomicsSummary:
    period: str = PERIOD_LIFETIME
    period_start: object = None
    period_end: object = None
    account_id: "int | None" = None
    symbol: "str | None" = None

    commission_revenue: Decimal = _ZERO
    commission_coverage: CategoryCoverage = None
    spread_revenue: Decimal = _ZERO
    spread_coverage: CategoryCoverage = None
    challenge_revenue: Decimal = _ZERO
    challenge_coverage: CategoryCoverage = None
    withdrawal_fee_revenue: Decimal = _ZERO
    withdrawal_fee_coverage: CategoryCoverage = None
    funded_profit_share_revenue: Decimal = _ZERO
    funded_profit_share_coverage: CategoryCoverage = None
    counterparty_pnl: Decimal = _ZERO
    counterparty_coverage: CategoryCoverage = None
    adjustments: Decimal = _ZERO
    adjustments_coverage: CategoryCoverage = None
    gross_broker_economic_result: Decimal = _ZERO

    pending_spread_estimate: PendingSpreadEstimate = None
    ib_expense: IBExpenseSummary = None
    retained: RetainedEconomicsSummary = None
    capital_flows: CapitalFlowsSummary = None


@dataclass
class IBExpenseGroupRow:
    group_by: str = ""
    group_key: str = ""
    group_label: str = ""
    pending: Decimal = _ZERO
    approved: Decimal = _ZERO
    credited: Decimal = _ZERO
    reversed_or_adjusted: Decimal = _ZERO
    net_paid: Decimal = _ZERO


def _sum_by_status(qs, field_name, status_field, status_value):
    v = qs.filter(**{status_field: status_value}).aggregate(t=Sum(field_name))["t"]
    return v if v is not None else _ZERO


def _ib_expense_summary(*, period, start, end, now, referral_id=None, rule_type=None) -> IBExpenseSummary:
    p_start, p_end = utc_period_window(period, now=now, start=start, end=end)

    obligations = IBCommissionObligation.objects.all()
    if p_start is not None:
        obligations = obligations.filter(created_at__gte=p_start)
    if p_end is not None:
        obligations = obligations.filter(created_at__lte=p_end)
    if referral_id is not None:
        obligations = obligations.filter(referral_id=referral_id)
    if rule_type is not None:
        obligations = obligations.filter(rule_type=rule_type)

    pending = _sum_by_status(obligations, "calculated_amount", "status", IBCommissionObligation.ST_PENDING)
    approved = _sum_by_status(obligations, "calculated_amount", "status", IBCommissionObligation.ST_APPROVED)
    credited = _sum_by_status(obligations, "calculated_amount", "status", IBCommissionObligation.ST_CREDITED)

    # Reversals/adjustments are attributed to WHEN they actually executed
    # (executed_at) — money only moves at EXECUTED — never to the
    # original obligation's own period. This is a deliberate choice: "net
    # paid IB expense in period P" means money that moved during P.
    adjustments_qs = IBCommissionAdjustment.objects.filter(status=IBCommissionAdjustment.ST_EXECUTED)
    if p_start is not None:
        adjustments_qs = adjustments_qs.filter(executed_at__gte=p_start)
    if p_end is not None:
        adjustments_qs = adjustments_qs.filter(executed_at__lte=p_end)
    if referral_id is not None:
        adjustments_qs = adjustments_qs.filter(referral_id=referral_id)
    reversed_or_adjusted = adjustments_qs.aggregate(t=Sum("amount"))["t"] or _ZERO

    net_paid = credited - reversed_or_adjusted

    return IBExpenseSummary(
        pending=pending, approved=approved, credited=credited,
        reversed_or_adjusted=reversed_or_adjusted, net_paid=net_paid,
        coverage=CategoryCoverage(
            COVERAGE_COMPLETE,
            "Aggregated directly from IBCommissionObligation/IBCommissionAdjustment "
            "(the certified IB lifecycle SSOT) — every row is counted exactly once, "
            "in its correct lifecycle bucket, under the period filter given.",
        ),
    )


def ib_expense_breakdown(
    *, group_by: str, period: str = PERIOD_LIFETIME, start=None, end=None, now=None,
) -> list[IBExpenseGroupRow]:
    """
    Disaggregate IB expense by one dimension. Supported group_by values:
      "referral"  — always fully attributable (direct FK on every obligation)
      "rule_type" — always fully attributable (direct field)
      "symbol"    — attributable ONLY for PER_LOT/TRADING_COMMISSION_REVENUE_SHARE/
                    SPREAD_REVENUE_SHARE (their source event carries a real symbol).
                    CHALLENGE_PERCENT/DEPOSIT_PERCENT obligations are grouped
                    under the explicit "NOT_ATTRIBUTABLE" key — never dropped,
                    never guessed.
    Raises ValueError for any other group_by value — this function never
    silently falls back to an unsupported grouping.
    """
    if group_by not in ("referral", "rule_type", "symbol"):
        raise ValueError(f"unsupported group_by: {group_by!r}")

    p_start, p_end = utc_period_window(period, now=now, start=start, end=end)
    obligations = IBCommissionObligation.objects.select_related("referral").all()
    if p_start is not None:
        obligations = obligations.filter(created_at__gte=p_start)
    if p_end is not None:
        obligations = obligations.filter(created_at__lte=p_end)

    adjustments_qs = IBCommissionAdjustment.objects.filter(status=IBCommissionAdjustment.ST_EXECUTED)
    if p_start is not None:
        adjustments_qs = adjustments_qs.filter(executed_at__gte=p_start)
    if p_end is not None:
        adjustments_qs = adjustments_qs.filter(executed_at__lte=p_end)

    if group_by == "referral":
        return _breakdown_by_referral(obligations, adjustments_qs)
    if group_by == "rule_type":
        return _breakdown_by_rule_type(obligations, adjustments_qs)
    return _breakdown_by_symbol(obligations, adjustments_qs)


def _accumulate(rows: dict, key, label, obligation):
    row = rows.setdefault(key, IBExpenseGroupRow(group_key=str(key), group_label=label))
    if obligation.status == IBCommissionObligation.ST_PENDING:
        row.pending += obligation.calculated_amount
    elif obligation.status == IBCommissionObligation.ST_APPROVED:
        row.approved += obligation.calculated_amount
    elif obligation.status == IBCommissionObligation.ST_CREDITED:
        row.credited += obligation.calculated_amount


def _breakdown_by_referral(obligations, adjustments_qs) -> list[IBExpenseGroupRow]:
    rows: dict = {}
    for ob in obligations.iterator():
        label = ob.referral.code if ob.referral_id else f"referral#{ob.referral_id}"
        _accumulate(rows, ob.referral_id, label, ob)
    for adj in adjustments_qs.iterator():
        if adj.referral_id in rows:
            rows[adj.referral_id].reversed_or_adjusted += adj.amount
        else:
            rows[adj.referral_id] = IBExpenseGroupRow(
                group_key=str(adj.referral_id), group_label=adj.referral.code,
                reversed_or_adjusted=adj.amount,
            )
    for row in rows.values():
        row.group_by = "referral"
        row.net_paid = row.credited - row.reversed_or_adjusted
    return sorted(rows.values(), key=lambda r: r.group_key)


def _breakdown_by_rule_type(obligations, adjustments_qs) -> list[IBExpenseGroupRow]:
    labels = dict(IBCommissionRule.RULE_TYPE_CHOICES)
    rows: dict = {}
    for ob in obligations.iterator():
        _accumulate(rows, ob.rule_type, labels.get(ob.rule_type, ob.rule_type), ob)
    # Adjustments don't carry rule_type directly — resolve via their
    # linked obligation (never guessed, never approximated).
    for adj in adjustments_qs.select_related("obligation").iterator():
        rt = adj.obligation.rule_type
        if rt in rows:
            rows[rt].reversed_or_adjusted += adj.amount
        else:
            rows[rt] = IBExpenseGroupRow(
                group_key=rt, group_label=labels.get(rt, rt), reversed_or_adjusted=adj.amount,
            )
    for row in rows.values():
        row.group_by = "rule_type"
        row.net_paid = row.credited - row.reversed_or_adjusted
    return sorted(rows.values(), key=lambda r: r.group_key)


def _resolve_symbol_for_obligation(ob) -> "str | None":
    """Per-rule-type symbol resolution, mirroring BROKER-ECONOMICS-03
    FASE A's own dimension-availability table exactly. Returns None
    (never a guess) when the rule type's source event carries no symbol."""
    if ob.rule_type not in _SYMBOL_ACCOUNT_ATTRIBUTABLE_RULE_TYPES:
        return None
    if ob.rule_type == IBCommissionRule.RULE_PER_LOT:
        event = LotExecutionEvent.objects.filter(pk=ob.source_event_id).only("symbol").first()
        return event.symbol if event else None
    # TRADING_COMMISSION_REVENUE_SHARE / SPREAD_REVENUE_SHARE — source is a BrokerLedger row.
    from .models import BrokerLedger as _BL
    row = _BL.objects.filter(pk=ob.source_event_id).only("symbol").first()
    return row.symbol if row else None


def _breakdown_by_symbol(obligations, adjustments_qs) -> list[IBExpenseGroupRow]:
    rows: dict = {}
    for ob in obligations.iterator():
        symbol = _resolve_symbol_for_obligation(ob)
        key = symbol or _NOT_ATTRIBUTABLE_KEY
        label = symbol or _NOT_ATTRIBUTABLE_LABEL
        _accumulate(rows, key, label, ob)
    for adj in adjustments_qs.select_related("obligation").iterator():
        symbol = _resolve_symbol_for_obligation(adj.obligation)
        key = symbol or _NOT_ATTRIBUTABLE_KEY
        if key in rows:
            rows[key].reversed_or_adjusted += adj.amount
        else:
            rows[key] = IBExpenseGroupRow(
                group_key=key, group_label=symbol or _NOT_ATTRIBUTABLE_LABEL,
                reversed_or_adjusted=adj.amount,
            )
    for row in rows.values():
        row.group_by = "symbol"
        row.net_paid = row.credited - row.reversed_or_adjusted
    return sorted(rows.values(), key=lambda r: r.group_key)


def _capital_flows_summary(*, period, start, end, now) -> CapitalFlowsSummary:
    p_start, p_end = utc_period_window(period, now=now, start=start, end=end)

    deposits = Deposit.objects.filter(status__in=Deposit.CREDITED_STATUSES)
    if p_start is not None:
        deposits = deposits.filter(created_at__gte=p_start)
    if p_end is not None:
        deposits = deposits.filter(created_at__lte=p_end)
    deposits_total = deposits.aggregate(t=Sum("amount_usd"))["t"] or _ZERO

    withdrawals = WithdrawalRequest.objects.filter(status=WithdrawalRequest.STATUS_COMPLETED)
    if p_start is not None:
        withdrawals = withdrawals.filter(created_at__gte=p_start)
    if p_end is not None:
        withdrawals = withdrawals.filter(created_at__lte=p_end)
    withdrawals_total = withdrawals.aggregate(t=Sum("amount_usd"))["t"] or _ZERO

    return CapitalFlowsSummary(
        deposits_total=deposits_total, withdrawals_total=withdrawals_total,
        coverage=CategoryCoverage(
            COVERAGE_COMPLETE,
            "Confirmed deposits and completed withdrawals, principal amount only — "
            "capital movement, never revenue. Kept structurally separate from every "
            "revenue/economics field in this module.",
        ),
    )


def _pending_spread_estimate(*, period, start, end, now, symbol=None) -> PendingSpreadEstimate:
    p_start, p_end = utc_period_window(period, now=now, start=start, end=end)

    events = LotExecutionEvent.objects.filter(entry_path=LotExecutionEvent.ENTRY_PENDING_TRIGGER)
    if p_start is not None:
        events = events.filter(created_at__gte=p_start)
    if p_end is not None:
        events = events.filter(created_at__lte=p_end)
    if symbol is not None:
        events = events.filter(symbol=symbol)

    trigger_open_count = events.count()
    if trigger_open_count == 0:
        return PendingSpreadEstimate(
            trigger_open_count=0, estimated_foregone_revenue=_ZERO,
            coverage=CategoryCoverage(
                COVERAGE_COMPLETE,
                "No pending/stop/limit-triggered opens in this scope — nothing foregone to estimate.",
            ),
        )

    from .spread_engine import calculate_spread_revenue

    configs = {c.symbol: c for c in BrokerSpreadConfig.objects.filter(enabled=True)}
    total_estimate = _ZERO
    missing_symbols = set()
    for ev in events.only("symbol", "qty").iterator():
        cfg = configs.get(ev.symbol)
        if cfg is None:
            missing_symbols.add(ev.symbol)
            continue
        total_estimate += Decimal(str(calculate_spread_revenue(ev.symbol, float(ev.qty), float(cfg.spread_pips))))

    if missing_symbols:
        note = (
            f"ESTIMATE / NOT BOOKED REVENUE. Conservative floor using each symbol's "
            f"CURRENT BrokerSpreadConfig.spread_pips only (no per-account markup override, "
            f"no dynamic multiplier chain — both require live, per-connection state this "
            f"module never resolves). No config found for: {sorted(missing_symbols)} — "
            f"those trades' foregone revenue is NOT included in this total."
        )
        coverage_status = COVERAGE_PARTIAL
    else:
        note = (
            "ESTIMATE / NOT BOOKED REVENUE. Conservative floor using each symbol's CURRENT "
            "BrokerSpreadConfig.spread_pips only (no per-account markup override, no dynamic "
            "multiplier chain). Never added to gross_broker_economic_result."
        )
        coverage_status = COVERAGE_PARTIAL  # always PARTIAL by design — this is an estimate, never COMPLETE

    return PendingSpreadEstimate(
        trigger_open_count=trigger_open_count,
        estimated_foregone_revenue=total_estimate,
        coverage=CategoryCoverage(coverage_status, note),
    )


def broker_economics_summary(
    *, period: str = PERIOD_LIFETIME, start=None, end=None,
    account_id: "int | None" = None, symbol: "str | None" = None, now=None,
) -> BrokerEconomicsSummary:
    """
    The single top-level entry point. Composes broker_pnl.py (unmodified)
    with the IB expense, capital flows, and pending-spread-estimate
    layers above, plus a Retained Broker Economics computation that is
    honest about its own known-incomplete cost coverage.

    Read-only: this function's own body contains zero .create()/.save()/
    .update() calls, on any model.
    """
    breakdown = broker_pnl.calculate_broker_pnl(
        period=period, start=start, end=end, account_id=account_id, symbol=symbol, now=now,
    )

    counterparty_coverage = (
        CategoryCoverage(COVERAGE_COMPLETE, "Every closed Trade in scope has a linked REV_COUNTERPARTY_PNL row.")
        if breakdown.coverage_pct >= 100.0
        else CategoryCoverage(
            COVERAGE_PARTIAL,
            f"{breakdown.missing_counterpart_count} of {breakdown.closed_trade_count} closed trades in scope "
            f"predate BOOK-02 and have no linked BrokerLedger row — coverage_pct={breakdown.coverage_pct}%. "
            "counterparty_pnl below is the sum of what exists, never topped up.",
        )
    )

    ib_expense = _ib_expense_summary(period=period, start=start, end=end, now=now)
    capital_flows = _capital_flows_summary(period=period, start=start, end=end, now=now)
    pending_spread_estimate = _pending_spread_estimate(period=period, start=start, end=end, now=now, symbol=symbol)

    # Retained Broker Economics — PARTIAL by construction today: 3 of its
    # 4 subtracted cost categories have no data source in this codebase
    # at all (BROKER-ECONOMICS-03 FASE A section 7).
    known_execution_liquidity_costs = _ZERO
    known_payment_transaction_costs = _ZERO
    other_captured_variable_costs = _ZERO
    retained_economics_value = (
        breakdown.broker_net_pnl
        - ib_expense.net_paid
        - known_execution_liquidity_costs
        - known_payment_transaction_costs
        - other_captured_variable_costs
    )
    retained = RetainedEconomicsSummary(
        gross_broker_economic_result=breakdown.broker_net_pnl,
        net_paid_ib_expense=ib_expense.net_paid,
        known_execution_liquidity_costs=known_execution_liquidity_costs,
        known_payment_transaction_costs=known_payment_transaction_costs,
        other_captured_variable_costs=other_captured_variable_costs,
        retained_economics=retained_economics_value,
        status=COVERAGE_PARTIAL,
        coverage=CategoryCoverage(
            COVERAGE_PARTIAL,
            "Execution/liquidity cost, payment/transaction cost, and other variable cost "
            "have no data source anywhere in this codebase yet (BOOK-04/05/06 remain "
            "simulated; no payment-processor cost is ever captured) — all three are "
            "included as $0.00 by construction, not because they are actually zero. "
            "This figure must never be presented as final/true net profit.",
        ),
    )

    return BrokerEconomicsSummary(
        period=period, period_start=breakdown.period_start, period_end=breakdown.period_end,
        account_id=account_id, symbol=symbol,

        commission_revenue=breakdown.commission,
        commission_coverage=CategoryCoverage(
            COVERAGE_COMPLETE, "Charged on every open path (market and pending/stop/limit-trigger alike).",
        ),
        spread_revenue=breakdown.spread,
        spread_coverage=CategoryCoverage(
            COVERAGE_PARTIAL,
            "Charged on the manual/market open path only. Zero markup on pending/stop/limit-"
            "trigger opens is a deliberate, documented design (BROKER-ECONOMICS-02A-0/03 "
            "Decision C) — CURRENT STATE, not changed by this module. See pending_spread_estimate.",
        ),
        challenge_revenue=breakdown.challenge_fee,
        challenge_coverage=CategoryCoverage(
            COVERAGE_PARTIAL,
            "BROKER-ECONOMICS-04A: the certified writer "
            "(challenge_revenue.py::record_challenge_fee_revenue()) is LIVE and forward-only "
            "from its committed call sites — every new certified challenge purchase since then "
            "produces exactly one DB-idempotent REV_CHALLENGE_FEE row. This sum may also "
            "include historical/legacy rows with source_challenge_enrollment=NULL, predating "
            "that writer, whose origin cannot be independently verified from this figure alone. "
            "PARTIAL reflects that historical-provenance ambiguity, not a gap in current writer "
            "coverage.",
        ),
        withdrawal_fee_revenue=breakdown.withdraw_fee,
        withdrawal_fee_coverage=CategoryCoverage(
            COVERAGE_PARTIAL,
            "BROKER-ECONOMICS-04A: the certified writer "
            "(withdrawal_economics.py::record_withdrawal_fee_revenue()) is LIVE, booking "
            "exactly at WithdrawalRequest COMPLETED, forward-only. This sum may also include "
            "historical/legacy rows with source_withdrawal=NULL, predating that writer. The "
            "withdrawal fee policy remains configurable (WithdrawalFeeConfig), never hardcoded. "
            "Provider/network cost remains UNKNOWN — this figure is gross fee revenue, never "
            "withdrawal profit.",
        ),
        funded_profit_share_revenue=breakdown.funded_profit_share,
        funded_profit_share_coverage=CategoryCoverage(
            COVERAGE_PARTIAL,
            "BROKER-ECONOMICS-04B: the certified writer "
            "(funded_economics.py::record_funded_broker_cut_revenue()) is LIVE, booking "
            "exactly at FundedPayoutRequest COMPLETED, forward-only, for both FUNDED_SIM and "
            "FUNDED_INTERNAL. Amount is read exclusively from the immutable "
            "FundedPayoutRequest.broker_cut snapshot, never recomputed. This is a SEPARATE "
            "economic fact from counterparty_pnl below, not a duplicate of it — see "
            "funded_economics.py's own module docstring for the full double-counting proof "
            "(REV_COUNTERPARTY_PNL already records the full trading result; this category "
            "records only the firm's subsequent contractual reclaim of broker_cut). PARTIAL "
            "because this sum has no historical/legacy rows predating the writer (forward-only, "
            "no backfill) and provider/network cost for the FUNDED_INTERNAL payout leg remains "
            "UNKNOWN, same limitation as withdrawal_fee_coverage.",
        ),
        counterparty_pnl=breakdown.counterparty_pnl,
        counterparty_coverage=counterparty_coverage,
        adjustments=breakdown.adjustments,
        adjustments_coverage=CategoryCoverage(
            COVERAGE_COMPLETE,
            "Every BrokerLedger.REV_ADJUSTMENT row that exists is summed correctly, signed. "
            "The certified writer (create_broker_economic_adjustment(), BROKER-ECONOMICS-02B) "
            "has never been invoked by a real Owner through the 02C UI — any non-zero figure "
            "here predates that service and is not attributable to it.",
        ),
        gross_broker_economic_result=breakdown.broker_net_pnl,

        pending_spread_estimate=pending_spread_estimate,
        ib_expense=ib_expense,
        retained=retained,
        capital_flows=capital_flows,
    )
