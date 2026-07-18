"""
simulator/broker_alerts.py
RISK-03 — Broker Risk Alerts & Dealing Dashboard Foundation.

Pure operational-intelligence layer: OBSERVE. EVALUATE. CLASSIFY.
REPORT. This module never blocks an order, never mutates any
financial/account/position row, and never writes anything itself — the
one adjacent write (BrokerRiskLock.last_recreated_at) happens in
consumers.py under RISK-02's own lock; this module only reads it.

Single source of truth for "is something dangerous happening right
now, broker-wide." Consumed by the Broker Control Center (admin.py),
broker_monitoring.py, and the admin BrokerRiskAlert display — none of
those should re-implement detection logic, only call collect_risk_alerts().

Data sources (never re-derived here):
    - simulator/broker_exposure.py  (RISK-01) — exposure, pricing
      coverage, concentration. The canonical, contract_size-correct,
      fresh-price-only numbers.
    - simulator/broker_risk.py      (RISK-02) — the actual configured
      broker-wide limits (RISK02_* settings) used both to gate new
      orders and, here, as the alert thresholds.
    - simulator/models.BrokerRiskLock — RISK-02's mutex singleton row.

FASE 1 audit (read-only, no modifications made to any of the modules
below):
    - exposure_engine.py already has an ad hoc, unstructured "risk_flags"
      list (CONCENTRATION/ONE_SIDED/TOXIC_ACTIVE, only HIGH/MEDIUM
      severities, no alert_id/status/threshold/metric fields), persisted
      into BrokerSnapshot.risk_flags and rendered by admin.py. It uses
      its own CONCENTRATION_RISK_PCT = 50.0 and ONE_SIDED_THRESHOLD = 0.90.
    - broker_monitoring.py independently defines MARGIN_WARN_PCT=200.0 /
      MARGIN_CRIT_PCT=130.0 (margin LEVEL %, lower = worse),
      EQUITY_LOSS_WARN_PCT=10.0 / EQUITY_LOSS_CRIT_PCT=25.0, and
      CONC_WARN_PCT=25.0 — a THIRD, different concentration threshold
      from exposure_engine's 50.0, plus its own alerts_summary()
      overall_status ("ok"/"warning"/"critical") computed only from
      margin + concentration, unaware of pricing coverage, RISK-02 limit
      proximity, or BrokerRiskLock health.
    - risk_engine.py separately defines _MARGIN_THRESHOLDS (used-%
      scale, WARNING=20/HIGH=50/DANGER=80 — the OPPOSITE scale and
      meaning from broker_monitoring's margin-level-% thresholds) and
      _EXPOSURE_THRESHOLDS (LOW=25/MEDIUM=60/HIGH=100), feeding
      per-account TraderScore classification — a different domain
      (trader routing classification, not broker-wide risk) entirely.
    - Nothing anywhere alerts on pricing coverage, BrokerRiskLock health,
      a risk-engine kill switch, or RISK-02 limit misconfiguration — all
      net-new ground for this module.
    - None of the above duplications/inconsistencies are modified by
      RISK-03: this module is strictly additive. It deliberately does
      NOT reuse exposure_engine's 50%/90% or broker_monitoring's 25%/
      200%/130%/10%/25% constants (those remain whatever those older,
      independent read-only dashboards already do) — RISK-03's own
      concentration/exposure/pricing thresholds below are new,
      independently configured constants specified by this block's spec.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("simulator.broker_alerts")

_ZERO = Decimal("0")


def _setting(name: str, default):
    return getattr(settings, name, default)


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — Severity (no magic strings: always reference Severity.X)
# ─────────────────────────────────────────────────────────────────────────
class Severity:
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    ORDER = (INFO, LOW, MEDIUM, HIGH, CRITICAL)          # ascending
    _RANK = {name: i for i, name in enumerate(ORDER)}

    @classmethod
    def rank(cls, severity: str) -> int:
        return cls._RANK[severity]


# ─────────────────────────────────────────────────────────────────────────
# FASE 4 — Category (at least: EXPOSURE, CONCENTRATION, PRICING, MARGIN,
# LIQUIDITY, EXECUTION, SYSTEM, CONFIGURATION). MARGIN/LIQUIDITY/EXECUTION
# have no active detector in RISK-03's initial rule set (none of the 12
# FASE 5 detections need them) — reserved for future blocks (e.g. BOOK-04
# routing/execution alerts) so the category space is stable now.
# ─────────────────────────────────────────────────────────────────────────
class Category:
    EXPOSURE = "EXPOSURE"
    CONCENTRATION = "CONCENTRATION"
    PRICING = "PRICING"
    MARGIN = "MARGIN"
    LIQUIDITY = "LIQUIDITY"
    EXECUTION = "EXECUTION"
    SYSTEM = "SYSTEM"
    CONFIGURATION = "CONFIGURATION"

    ALL = (EXPOSURE, CONCENTRATION, PRICING, MARGIN, LIQUIDITY,
           EXECUTION, SYSTEM, CONFIGURATION)


class Status:
    """Stateless-computation status. Every alert returned by this module
    is freshly evaluated on each call (no persistence, no lifecycle) —
    ACTIVE is the only status that can ever be true right now. Reserved
    for a future acknowledge/resolve workflow (e.g. BOOK-04) without
    requiring a BrokerRiskAlert field rename."""
    ACTIVE = "ACTIVE"


# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — BrokerRiskAlert domain model (deliberately NOT a Django model:
# nothing here is persisted; every call recomputes from live data, same
# as broker_exposure.py/broker_risk.py's dataclasses).
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BrokerRiskAlert:
    alert_id: str
    severity: str
    category: str
    title: str
    description: str
    status: str
    created_at: datetime
    source_module: str
    affected_symbol: Optional[str]
    affected_account: Optional[int]
    metric: str
    current_value: Optional[Decimal]
    threshold: Optional[Decimal]
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "source_module": self.source_module,
            "affected_symbol": self.affected_symbol,
            "affected_account": self.affected_account,
            "metric": self.metric,
            "current_value": _jsonable(self.current_value),
            "threshold": _jsonable(self.threshold),
            "metadata": self.metadata,
        }


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _make_alert(
    *, rule: str, category: str, severity: str, title: str, description: str,
    source_module: str, metric: str,
    current_value=None, threshold=None,
    affected_symbol: Optional[str] = None, affected_account: Optional[int] = None,
    metadata: Optional[dict] = None, now: Optional[datetime] = None,
) -> BrokerRiskAlert:
    """Builds a BrokerRiskAlert with a deterministic alert_id:
    category:rule[:symbol][:account]. Same underlying condition ->
    always the same alert_id, regardless of how many times/where this
    is called from — this IS the "no duplicates" guarantee (FASE 9),
    not a separate dedup pass bolted on afterward."""
    parts = [category, rule]
    if affected_symbol is not None:
        parts.append(str(affected_symbol))
    if affected_account is not None:
        parts.append(str(affected_account))
    alert_id = ":".join(parts)
    return BrokerRiskAlert(
        alert_id=alert_id,
        severity=severity,
        category=category,
        title=title,
        description=description,
        status=Status.ACTIVE,
        created_at=now or timezone.now(),
        source_module=source_module,
        affected_symbol=affected_symbol,
        affected_account=affected_account,
        metric=metric,
        current_value=current_value,
        threshold=threshold,
        metadata=metadata or {},
    )


# ─────────────────────────────────────────────────────────────────────────
# FASE 5 — detection thresholds. New, independent constants (see the
# module docstring's FASE 1 note: these do NOT replace or read from
# exposure_engine.py's / broker_monitoring.py's own thresholds).
# Overridable via Django settings, same pattern as broker_risk.py.
# ─────────────────────────────────────────────────────────────────────────
EXPOSURE_ALERT_MEDIUM_PCT = _setting("RISK03_EXPOSURE_ALERT_MEDIUM_PCT", Decimal("80"))
EXPOSURE_ALERT_HIGH_PCT = _setting("RISK03_EXPOSURE_ALERT_HIGH_PCT", Decimal("90"))
EXPOSURE_ALERT_CRITICAL_PCT = _setting("RISK03_EXPOSURE_ALERT_CRITICAL_PCT", Decimal("100"))

PRICING_ALERT_MEDIUM_PCT = _setting("RISK03_PRICING_ALERT_MEDIUM_PCT", Decimal("100"))
PRICING_ALERT_HIGH_PCT = _setting("RISK03_PRICING_ALERT_HIGH_PCT", Decimal("95"))

CONCENTRATION_ALERT_HIGH_PCT = _setting("RISK03_CONCENTRATION_ALERT_HIGH_PCT", Decimal("70"))
CONCENTRATION_ALERT_CRITICAL_PCT = _setting("RISK03_CONCENTRATION_ALERT_CRITICAL_PCT", Decimal("85"))

# Item 10 — no kill switch existed anywhere in the codebase before this
# block (confirmed by the FASE 1 grep sweep). RISK-03 introduces the
# flag itself so the detection is real rather than a no-op; defaults to
# enabled so a stock install never fires this.
RISK_ENGINE_ENABLED = bool(_setting("RISK03_RISK_ENGINE_ENABLED", True))

# Item 9 (post-review correction) — last_recreated_at is a durable
# HISTORICAL marker (never cleared, see the model docstring) — it must
# NOT produce an indefinite HIGH alert just because it is non-null.
# "Recently recreated" is only actionable as a live incident within this
# window; a recreation older than the window stops being surfaced as a
# recurring dashboard alert (the field itself is still visible to anyone
# inspecting BrokerRiskLock directly — this only gates the ALERT).
# Default: 1 hour — long enough to survive a deploy/investigation cycle,
# short enough that a months-old recreation doesn't sit as a permanent
# false "warning" on an otherwise healthy broker.
BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS = int(_setting(
    "RISK03_BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS", 3600
))

# Items 11/12 — sanity bounds for the 9 RISK-02 broker-wide limits
# (simulator/broker_risk.py). Deliberately wide order-of-magnitude bands
# — this is a misconfiguration safety net, not a second risk-limit
# engine, and every current broker_risk.py default sits safely inside
# its band (verified in test_broker_alerts_engine.py) so a stock install
# produces zero CONFIGURATION alerts.
_LOTS_SANE_MIN = _setting("RISK03_CONFIG_LOTS_SANE_MIN", Decimal("0.01"))
_LOTS_SANE_MAX = _setting("RISK03_CONFIG_LOTS_SANE_MAX", Decimal("100000000"))
_NOTIONAL_SANE_MIN = _setting("RISK03_CONFIG_NOTIONAL_SANE_MIN", Decimal("1"))
_NOTIONAL_SANE_MAX = _setting("RISK03_CONFIG_NOTIONAL_SANE_MAX", Decimal("1000000000000000"))
_COUNT_SANE_MIN = _setting("RISK03_CONFIG_COUNT_SANE_MIN", Decimal("1"))
_COUNT_SANE_MAX = _setting("RISK03_CONFIG_COUNT_SANE_MAX", Decimal("100000000"))

_CONFIG_LIMITS = (
    # (attribute name in broker_risk module, sane_min, sane_max)
    ("MAX_SYMBOL_EXPOSURE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_ACCOUNT_EXPOSURE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_TOTAL_BROKER_EXPOSURE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_LONG_EXPOSURE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_SHORT_EXPOSURE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_GROSS_NOTIONAL", _NOTIONAL_SANE_MIN, _NOTIONAL_SANE_MAX),
    ("MAX_NET_NOTIONAL", _NOTIONAL_SANE_MIN, _NOTIONAL_SANE_MAX),
    ("MAX_POSITION_SIZE_LOTS", _LOTS_SANE_MIN, _LOTS_SANE_MAX),
    ("MAX_OPEN_POSITIONS_BROKER_WIDE", _COUNT_SANE_MIN, _COUNT_SANE_MAX),
)

_SOURCE = "simulator.broker_alerts"


def _tiered_above(value: Decimal, tiers) -> Optional[tuple]:
    """tiers: descending list of (threshold, severity, rule_suffix).
    Returns the FIRST (highest) tier `value` STRICTLY exceeds (">"),
    matching the spec's own wording ("Exposure > 80%", "Concentración
    >70%"), or None. Mutually exclusive by design — one metric, at most
    one alert, escalating severity — never simultaneous alerts for the
    same number."""
    for threshold, severity, suffix in tiers:
        if value > threshold:
            return threshold, severity, suffix
    return None


def _tiered_below(value: Decimal, tiers) -> Optional[tuple]:
    """Same as _tiered_above but for "value STRICTLY below threshold"
    metrics (pricing coverage: "coverage <100%", "coverage <95%").
    `tiers` is ascending by threshold (the tightest/most-severe bound
    first) since lower coverage is worse."""
    for threshold, severity, suffix in tiers:
        if value < threshold:
            return threshold, severity, suffix
    return None


# ─────────────────────────────────────────────────────────────────────────
# FASE 6 — collectors
# ─────────────────────────────────────────────────────────────────────────
def collect_exposure_alerts(snapshot=None) -> list[BrokerRiskAlert]:
    """Items 1-3 (EXPOSURE: broker-wide gross lots vs RISK-02's
    MAX_TOTAL_BROKER_EXPOSURE_LOTS — the same broker-wide cap RISK-02
    itself enforces, see broker_risk.validate_total_limit) and items 6-7
    (CONCENTRATION: per-symbol share of gross notional, RISK-01's
    concentration_by_symbol)."""
    from . import broker_risk as _risk
    from .broker_exposure import broker_exposure_snapshot

    snapshot = snapshot or broker_exposure_snapshot()
    now = timezone.now()
    alerts: list[BrokerRiskAlert] = []

    limit = _risk.MAX_TOTAL_BROKER_EXPOSURE_LOTS
    if limit > 0:
        pct = (snapshot.gross_quantity / limit) * Decimal("100")
        hit = _tiered_above(pct, [
            (EXPOSURE_ALERT_CRITICAL_PCT, Severity.CRITICAL, "CRITICAL"),
            (EXPOSURE_ALERT_HIGH_PCT, Severity.HIGH, "HIGH"),
            (EXPOSURE_ALERT_MEDIUM_PCT, Severity.MEDIUM, "MEDIUM"),
        ])
        if hit:
            threshold, severity, suffix = hit
            alerts.append(_make_alert(
                rule=f"EXPOSURE_BROKER_TOTAL_{suffix}", category=Category.EXPOSURE,
                severity=severity,
                title=f"Broker-wide exposure at {pct:.1f}% of limit",
                description=(
                    f"Gross broker-wide exposure is {snapshot.gross_quantity} lots, "
                    f"{pct:.1f}% of the configured RISK02_MAX_TOTAL_BROKER_EXPOSURE_LOTS "
                    f"limit ({limit} lots)."
                ),
                source_module=_SOURCE + ".collect_exposure_alerts",
                metric="broker_gross_exposure_pct_of_limit",
                current_value=pct, threshold=threshold,
                metadata={"gross_quantity": float(snapshot.gross_quantity), "limit_lots": float(limit)},
                now=now,
            ))

    for symbol, pct in snapshot.concentration_by_symbol.items():
        hit = _tiered_above(pct, [
            (CONCENTRATION_ALERT_CRITICAL_PCT, Severity.CRITICAL, "CRITICAL"),
            (CONCENTRATION_ALERT_HIGH_PCT, Severity.HIGH, "HIGH"),
        ])
        if hit:
            threshold, severity, suffix = hit
            alerts.append(_make_alert(
                rule=f"CONCENTRATION_SYMBOL_{suffix}", category=Category.CONCENTRATION,
                severity=severity,
                title=f"{symbol} concentration at {pct:.1f}% of gross exposure",
                description=(
                    f"{symbol} represents {pct:.1f}% of broker-wide gross notional "
                    f"exposure — single-symbol concentration risk."
                ),
                source_module=_SOURCE + ".collect_exposure_alerts",
                metric="symbol_concentration_pct", affected_symbol=symbol,
                current_value=pct, threshold=threshold,
                now=now,
            ))

    return alerts


def collect_pricing_alerts(snapshot=None) -> list[BrokerRiskAlert]:
    """Items 4-5 (PRICING: RISK-01's pricing_coverage_pct)."""
    from .broker_exposure import broker_exposure_snapshot

    snapshot = snapshot or broker_exposure_snapshot()
    now = timezone.now()
    alerts: list[BrokerRiskAlert] = []

    pct = snapshot.pricing_coverage_pct
    hit = _tiered_below(pct, [
        (PRICING_ALERT_HIGH_PCT, Severity.HIGH, "HIGH"),
        (PRICING_ALERT_MEDIUM_PCT, Severity.MEDIUM, "MEDIUM"),
    ])
    if hit:
        threshold, severity, suffix = hit
        alerts.append(_make_alert(
            rule=f"PRICING_COVERAGE_{suffix}", category=Category.PRICING,
            severity=severity,
            title=f"Pricing coverage at {pct:.1f}%",
            description=(
                f"Only {pct:.1f}% of open positions broker-wide have a fresh "
                f"price ({snapshot.unpriced_position_count} unpriced position(s): "
                f"{', '.join(snapshot.stale_or_missing_symbols) or '—'}). "
                f"RISK-02 fails closed on notional-based limits while this holds."
            ),
            source_module=_SOURCE + ".collect_pricing_alerts",
            metric="pricing_coverage_pct", current_value=pct, threshold=threshold,
            metadata={
                "unpriced_position_count": snapshot.unpriced_position_count,
                "stale_or_missing_symbols": list(snapshot.stale_or_missing_symbols),
            },
            now=now,
        ))

    return alerts


def collect_configuration_alerts() -> list[BrokerRiskAlert]:
    """Items 11-12 (CONFIGURATION: RISK-02 limits configured absurdly
    low — may effectively block all order flow — or absurdly high —
    provides negligible protection)."""
    from . import broker_risk as _risk

    now = timezone.now()
    alerts: list[BrokerRiskAlert] = []

    for attr, sane_min, sane_max in _CONFIG_LIMITS:
        value = Decimal(str(getattr(_risk, attr)))
        if value < sane_min:
            alerts.append(_make_alert(
                rule=f"{attr}_TOO_LOW", category=Category.CONFIGURATION,
                severity=Severity.HIGH,
                title=f"{attr} configured absurdly low ({value})",
                description=(
                    f"broker_risk.{attr} is set to {value}, below the sane "
                    f"floor of {sane_min}. This may effectively block all "
                    f"order flow — verify this is intentional."
                ),
                source_module=_SOURCE + ".collect_configuration_alerts",
                metric=f"{attr.lower()}_value", current_value=value, threshold=sane_min,
                now=now,
            ))
        elif value > sane_max:
            alerts.append(_make_alert(
                rule=f"{attr}_TOO_HIGH", category=Category.CONFIGURATION,
                severity=Severity.MEDIUM,
                title=f"{attr} configured absurdly high ({value})",
                description=(
                    f"broker_risk.{attr} is set to {value}, above the sane "
                    f"ceiling of {sane_max}. This limit provides negligible "
                    f"real protection at this value."
                ),
                source_module=_SOURCE + ".collect_configuration_alerts",
                metric=f"{attr.lower()}_value", current_value=value, threshold=sane_max,
                now=now,
            ))

    return alerts


def collect_system_alerts() -> list[BrokerRiskAlert]:
    """Items 8-9-10 (SYSTEM: BrokerRiskLock existence/recreation, risk
    engine enabled flag). Pure read — never creates/self-heals the lock
    row itself; that stays exclusively consumers.py's job."""
    from .models import BrokerRiskLock

    now = timezone.now()
    alerts: list[BrokerRiskAlert] = []

    lock_row = BrokerRiskLock.objects.filter(pk=1).first()
    if lock_row is None:
        alerts.append(_make_alert(
            rule="BROKER_RISK_LOCK_MISSING", category=Category.SYSTEM,
            severity=Severity.CRITICAL,
            title="BrokerRiskLock singleton row is missing right now",
            description=(
                "The RISK-02 broker-wide risk mutex (BrokerRiskLock id=1) is "
                "missing at this moment. This is a self-healing condition: "
                "the next order-open call will transparently recreate the "
                "row and continue operating correctly — this is NOT a "
                "permanent or irreversible loss of protection. It IS an "
                "operational anomaly (the row should only ever be absent "
                "after an out-of-band table truncation) and should be "
                "investigated to confirm nothing else was affected."
            ),
            source_module=_SOURCE + ".collect_system_alerts",
            metric="broker_risk_lock_exists", current_value=0, threshold=1,
            now=now,
        ))
    elif lock_row.last_recreated_at is not None:
        delta_seconds = (now - lock_row.last_recreated_at).total_seconds()
        if delta_seconds < 0:
            # Correction 1 — a last_recreated_at in the future can only be
            # clock skew or a corrupted write; report it as its own
            # anomaly rather than silently treating it as "not recent"
            # (a negative delta would otherwise never satisfy <= window).
            alerts.append(_make_alert(
                rule="BROKER_RISK_LOCK_TIMESTAMP_ANOMALY", category=Category.SYSTEM,
                severity=Severity.HIGH,
                title="BrokerRiskLock.last_recreated_at is in the future",
                description=(
                    f"BrokerRiskLock.last_recreated_at ({lock_row.last_recreated_at.isoformat()}) "
                    f"is ahead of the current server time ({now.isoformat()}) by "
                    f"{-delta_seconds:.0f}s. This points to clock skew between "
                    f"processes/hosts or a corrupted write — investigate before "
                    f"trusting any time-based comparison against this field."
                ),
                source_module=_SOURCE + ".collect_system_alerts",
                metric="broker_risk_lock_last_recreated_at_anomaly",
                metadata={
                    "last_recreated_at": lock_row.last_recreated_at.isoformat(),
                    "now": now.isoformat(),
                    "delta_seconds": delta_seconds,
                },
                now=now,
            ))
        elif delta_seconds <= BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS:
            # Correction 1 — only a RECENT recreation is a live incident.
            # last_recreated_at itself is never cleared (see the model
            # docstring) — this only gates whether it still produces an
            # active alert; boundary is inclusive (<=), tested explicitly.
            alerts.append(_make_alert(
                rule="BROKER_RISK_LOCK_RECREATED", category=Category.SYSTEM,
                severity=Severity.HIGH,
                title="BrokerRiskLock singleton was recently auto-recreated",
                description=(
                    f"The BrokerRiskLock row was found missing and auto-recreated "
                    f"at {lock_row.last_recreated_at.isoformat()} ({delta_seconds:.0f}s "
                    f"ago) by the self-healing path in _db_open_position_atomic. "
                    f"The broker is operating correctly right now — this flags a "
                    f"recent operational anomaly (the row should only ever be "
                    f"lost after an out-of-band table truncation) for investigation, "
                    f"not an ongoing loss of protection."
                ),
                source_module=_SOURCE + ".collect_system_alerts",
                metric="broker_risk_lock_last_recreated_at",
                metadata={
                    "last_recreated_at": lock_row.last_recreated_at.isoformat(),
                    "seconds_ago": delta_seconds,
                    "alert_window_seconds": BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS,
                },
                now=now,
            ))
        # else: recreation is older than the window — no active alert.
        # last_recreated_at stays on the row (never cleared) for anyone
        # inspecting BrokerRiskLock directly.

    if not RISK_ENGINE_ENABLED:
        alerts.append(_make_alert(
            rule="RISK_ENGINE_DISABLED", category=Category.SYSTEM,
            severity=Severity.CRITICAL,
            title="Broker risk engine is disabled",
            description=(
                "settings.RISK03_RISK_ENGINE_ENABLED is False — RISK-02 order "
                "validation and RISK-03 alerting both assume the risk engine "
                "is active. Re-enable it unless this is a deliberate, "
                "time-boxed maintenance window."
            ),
            source_module=_SOURCE + ".collect_system_alerts",
            metric="risk_engine_enabled", current_value=0, threshold=1,
            now=now,
        ))

    return alerts


# Correction 2 — collector registry: (module-global function NAME, label
# used for the ALERT_COLLECTOR_FAILURE alert_id/title). Looked up by name
# via globals() at call time (not by direct function reference) so
# unittest.mock.patch.object(broker_alerts, "collect_configuration_alerts", ...)
# keeps working exactly as it did before this correction.
_COLLECTOR_NAMES = (
    ("collect_exposure_alerts", "EXPOSURE_COLLECTOR"),
    ("collect_pricing_alerts", "PRICING_COLLECTOR"),
    ("collect_configuration_alerts", "CONFIGURATION_COLLECTOR"),
    ("collect_system_alerts", "SYSTEM_COLLECTOR"),
)


def collect_risk_alerts() -> list[BrokerRiskAlert]:
    """FASE 6 — the central aggregator. Runs every sub-collector,
    concatenates, and returns a list ordered by severity (CRITICAL
    first), then category, then alert_id for a fully deterministic,
    duplicate-free ordering.

    Correction 2 (post-review) — a failing sub-collector is isolated (it
    never crashes collect_risk_alerts or blocks the others) but is NEVER
    silently swallowed: the full exception is logged (exc_info=True) AND
    a structured, fail-visible BrokerRiskAlert (category=SYSTEM,
    severity=HIGH, deterministic alert_id per collector — never
    duplicated across calls) is added so the failure itself shows up on
    the dashboard. Its metadata carries only the exception TYPE and a
    timestamp — never str(exc) or a traceback — so no exception message
    (which could echo internal state) ever reaches a public surface;
    the full traceback is exclusively in the server log."""
    from .broker_exposure import broker_exposure_snapshot

    snapshot = broker_exposure_snapshot()
    now = timezone.now()
    alerts: list[BrokerRiskAlert] = []
    module_globals = globals()
    args_by_name = {
        "collect_exposure_alerts": (snapshot,),
        "collect_pricing_alerts": (snapshot,),
        "collect_configuration_alerts": (),
        "collect_system_alerts": (),
    }

    for fn_name, label in _COLLECTOR_NAMES:
        collector = module_globals[fn_name]
        try:
            alerts.extend(collector(*args_by_name[fn_name]))
        except Exception as exc:
            log.error("[broker_alerts] collector=%s failed", fn_name, exc_info=True)
            alerts.append(_make_alert(
                rule=f"ALERT_COLLECTOR_FAILURE_{label}", category=Category.SYSTEM,
                severity=Severity.HIGH,
                title=f"{label} failed to evaluate",
                description=(
                    f"The {label} raised {type(exc).__name__} while evaluating "
                    f"— alerts from its category may be incomplete or missing "
                    f"until this is resolved. See the server log "
                    f"(logger simulator.broker_alerts) for the full traceback; "
                    f"it is deliberately not included here."
                ),
                source_module=_SOURCE + "." + fn_name,
                metric="alert_collector_failure",
                metadata={
                    "collector": fn_name,
                    "exception_type": type(exc).__name__,
                    "failed_at": now.isoformat(),
                },
                now=now,
            ))

    alerts.sort(key=lambda a: (-Severity.rank(a.severity), a.category, a.alert_id))
    return alerts


def broker_health_summary() -> dict:
    """FASE 8 support — the small, ready-to-render payload the Dealing
    Dashboard's Broker Health panel needs, without the panel having to
    know anything about BrokerRiskAlert internals."""
    from .broker_exposure import broker_exposure_snapshot

    alerts = collect_risk_alerts()
    snapshot = broker_exposure_snapshot()

    has_critical = any(a.severity == Severity.CRITICAL for a in alerts)
    has_warning = any(a.severity in (Severity.HIGH, Severity.MEDIUM) for a in alerts)
    status = "critical" if has_critical else ("warning" if has_warning else "healthy")

    top_critical = [a.to_dict() for a in alerts if a.severity == Severity.CRITICAL][:5]

    return {
        "status": status,
        "alert_count": len(alerts),
        "critical_count": sum(1 for a in alerts if a.severity == Severity.CRITICAL),
        "high_count": sum(1 for a in alerts if a.severity == Severity.HIGH),
        "medium_count": sum(1 for a in alerts if a.severity == Severity.MEDIUM),
        "top_critical_alerts": top_critical,
        "exposure_gross_quantity": float(snapshot.gross_quantity),
        "pricing_coverage_pct": float(snapshot.pricing_coverage_pct),
        "largest_symbol": snapshot.largest_symbol,
        "largest_symbol_concentration_pct": float(
            snapshot.concentration_by_symbol.get(snapshot.largest_symbol, _ZERO)
        ) if snapshot.largest_symbol else 0.0,
        "generated_at": timezone.now().isoformat(),
    }
