"""
simulator/commercial_pricing.py — SPREAD-04.

Single, explicit resolution of commercial pricing policy — spread markup,
commission per-lot, commission pct, and floor/ceiling — for every account
type (Demo, Real, Challenge, Funded). Replaces the dispersed, silent
fallbacks previously spread across consumers.py::commission_for() and
spread_engine.broker_price()'s bare markup_pips parameter. One resolver
for all account types — no per-account-type branches duplicated elsewhere.

Two-layer design, the same split established in pricing_context.py
(SPREAD-02) and spread_config_cache.py (SPREAD-03):

  - resolve_commercial_pricing_fields(account) — DB-aware. Called ONCE per
    connection, at hydrate time (a sync context — e.g. inside
    TradingConsumer._db_read_account, itself @database_sync_to_async).
    Walks a fixed priority chain and returns a plain dict of ACCOUNT-level
    fields. Never called per-tick.

      A. commercial_profile_snapshot (frozen at account creation — the
         normal case for every account created after this block).
      B. Legacy flat snapshot fields (spread_pips_snapshot /
         commission_per_lot_snapshot) — pre-SPREAD-04 AccountProduct-linked
         accounts that predate the JSON snapshot; reconstructed into an
         equivalent profile, never backfilled in the DB.
      C. account.account_product — live FK with no snapshot at all
         (defensive; every current creation path already freezes one).
      D. The real ChallengeEnrollment relation (phase1/phase2/funded) —
         CHALLENGE and FUNDED accounts, resolved from ChallengeProduct.
      E. Explicit, observable fallback — logs a structured warning; never
         a silent zero.

  - build_commercial_pricing_profile(account_fields, symbol) — pure,
    DB-free, safe every tick. Combines the cached account-level fields
    (layer above) with the SYMBOL-level floor/ceiling read from
    simulator.spread_config_cache (SPREAD-03's async-safe cache — not a
    new DB read) into one immutable CommercialPricingProfile. An
    account/product-level min/max override (if set) wins over the
    symbol's own BrokerSpreadConfig floor/ceiling.

commercial_pricing_fields_from_account_product() and
commercial_pricing_fields_from_challenge_product() are the SAME functions
the resolver's fallback branches (B/D) use internally — exposed publicly
so the account-creation call sites (views.py::create_account_view,
challenge_engine.py) freeze the *identical* computation into
commercial_profile_snapshot, instead of recomputing it a second way.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger("simulator.spread")

PROFILE_SCHEMA_VERSION = 1

SOURCE_ACCOUNT_SNAPSHOT        = "account_snapshot"
SOURCE_ACCOUNT_SNAPSHOT_LEGACY = "account_snapshot_legacy"
SOURCE_ACCOUNT_PRODUCT         = "account_product"
SOURCE_CHALLENGE_PRODUCT       = "challenge_product"
SOURCE_LEGACY_FALLBACK         = "legacy_fallback"
SOURCE_CAPTURE_FAILED          = "capture_failed"


@dataclass(frozen=True)
class CommercialPricingProfile:
    profile_version: int
    profile_id: str
    account_type: Optional[str]
    product_type: Optional[str]
    spread_markup_pips: float
    commission_per_lot: float
    commission_pct: float
    min_spread_pips: Optional[float]
    max_spread_pips: Optional[float]
    enabled: bool
    source: str

    def __post_init__(self) -> None:
        if self.min_spread_pips is not None and self.max_spread_pips is not None:
            if self.min_spread_pips > self.max_spread_pips:
                raise ValueError(
                    f"min_spread_pips ({self.min_spread_pips}) must be <= "
                    f"max_spread_pips ({self.max_spread_pips})"
                )

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_fields(
    *, profile_id, account_type, product_type,
    spread_markup_pips, commission_per_lot, commission_pct,
    min_spread_pips, max_spread_pips, enabled, source,
) -> dict:
    return {
        "profile_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "account_type": account_type,
        "product_type": product_type,
        "spread_markup_pips": _safe_float(spread_markup_pips) or 0.0,
        "commission_per_lot": _safe_float(commission_per_lot) or 0.0,
        "commission_pct": _safe_float(commission_pct) or 0.0,
        "min_spread_pips": _safe_float(min_spread_pips),
        "max_spread_pips": _safe_float(max_spread_pips),
        "enabled": bool(enabled),
        "source": source,
    }


def commercial_pricing_fields_from_account_product(product) -> dict:
    """The single computation used both by the resolver's fallback branch
    and by views.py::create_account_view when freezing a new account's
    commercial_profile_snapshot — never duplicated."""
    return _build_fields(
        profile_id=f"account_product:{product.pk}",
        account_type=None,
        product_type=product.product_type,
        spread_markup_pips=product.typical_spread_pips,
        commission_per_lot=product.commission_per_lot,
        commission_pct=product.commission_pct,
        min_spread_pips=None,
        max_spread_pips=None,
        enabled=product.is_active,
        source=SOURCE_ACCOUNT_PRODUCT,
    )


def commercial_pricing_fields_from_challenge_product(product) -> dict:
    """The single computation used both by the resolver's fallback branch
    and by challenge_engine.py when freezing a new CHALLENGE/FUNDED
    account's commercial_profile_snapshot — never duplicated."""
    return _build_fields(
        profile_id=f"challenge_product:{product.pk}",
        account_type=None,
        product_type="CHALLENGE",
        spread_markup_pips=product.spread_markup_pips,
        commission_per_lot=product.commission_per_lot,
        commission_pct=product.commission_pct,
        min_spread_pips=product.min_spread_pips,
        max_spread_pips=product.max_spread_pips,
        enabled=product.is_active,
        source=SOURCE_CHALLENGE_PRODUCT,
    )


def _fields_from_legacy_flat_snapshot(account) -> dict:
    return _build_fields(
        profile_id=f"account_snapshot_legacy:{account.pk}",
        account_type=account.account_type,
        product_type=account.product_code_snapshot,
        spread_markup_pips=account.spread_pips_snapshot,
        commission_per_lot=account.commission_per_lot_snapshot,
        commission_pct=None,  # did not exist as a flat column before SPREAD-04
        min_spread_pips=None,
        max_spread_pips=None,
        enabled=True,
        source=SOURCE_ACCOUNT_SNAPSHOT_LEGACY,
    )


def _find_challenge_product(account):
    """Resolves the ChallengeProduct that produced *account*, via the real
    ChallengeEnrollment relation (phase1/phase2/funded) — one query, no
    reliance on OneToOneField reverse-accessor exceptions."""
    from django.db.models import Q
    from .models import ChallengeEnrollment

    enrollment = (
        ChallengeEnrollment.objects
        .filter(Q(phase1_account=account) | Q(phase2_account=account) | Q(funded_account=account))
        .select_related("product")
        .first()
    )
    return enrollment.product if enrollment else None


def _legacy_fallback_fields(account) -> dict:
    logger.warning(
        "event=commercial_pricing_profile_missing account_id=%s account_type=%s "
        "— no commercial_profile_snapshot, no legacy snapshot, no account_product, "
        "no challenge relation; falling back to zero markup / spec.commission_pct",
        account.pk, account.account_type,
    )
    return _build_fields(
        profile_id="legacy_fallback",
        account_type=account.account_type,
        product_type=None,
        spread_markup_pips=0.0,
        commission_per_lot=0.0,
        commission_pct=0.0,
        min_spread_pips=None,
        max_spread_pips=None,
        enabled=True,
        source=SOURCE_LEGACY_FALLBACK,
    )


def resolve_commercial_pricing_fields(account) -> dict:
    """DB-aware. Call once per connection (hydrate time), never per-tick.
    Never raises — any internal failure degrades to the same explicit,
    logged fallback as "nothing resolved"."""
    try:
        if account.commercial_profile_snapshot is not None:
            return dict(account.commercial_profile_snapshot)

        if account.spread_pips_snapshot is not None or account.commission_per_lot_snapshot is not None:
            return _fields_from_legacy_flat_snapshot(account)

        if account.account_product_id:
            return commercial_pricing_fields_from_account_product(account.account_product)

        challenge_product = _find_challenge_product(account)
        if challenge_product is not None:
            return commercial_pricing_fields_from_challenge_product(challenge_product)

        return _legacy_fallback_fields(account)
    except Exception as exc:
        logger.warning(
            "event=commercial_pricing_profile_resolution_failed account_id=%s error=%r "
            "— falling back to zero markup / spec.commission_pct",
            getattr(account, "pk", None), exc,
        )
        return _build_fields(
            profile_id="legacy_fallback", account_type=getattr(account, "account_type", None),
            product_type=None, spread_markup_pips=0.0, commission_per_lot=0.0, commission_pct=0.0,
            min_spread_pips=None, max_spread_pips=None, enabled=True, source=SOURCE_LEGACY_FALLBACK,
        )


def build_commercial_pricing_profile(account_fields: dict, symbol: str) -> CommercialPricingProfile:
    """Pure, DB-free — safe every tick. account_fields is whatever
    resolve_commercial_pricing_fields() returned, cached by the caller
    (e.g. TradingConsumer.account["commercial_pricing_fields"]). Reads
    ONLY simulator.spread_config_cache (an in-memory dict, not the DB) for
    the symbol's floor/ceiling. An account/product-level override (from
    account_fields) wins over the symbol's own BrokerSpreadConfig
    min/max. Never raises."""
    try:
        from .spread_config_cache import get_cached_config

        cfg = get_cached_config(symbol)
        min_pips = account_fields.get("min_spread_pips")
        max_pips = account_fields.get("max_spread_pips")
        if min_pips is None and cfg is not None:
            min_pips = cfg.min_spread
        if max_pips is None and cfg is not None:
            max_pips = cfg.max_spread

        return CommercialPricingProfile(
            profile_version=account_fields.get("profile_version", PROFILE_SCHEMA_VERSION),
            profile_id=account_fields.get("profile_id", SOURCE_LEGACY_FALLBACK),
            account_type=account_fields.get("account_type"),
            product_type=account_fields.get("product_type"),
            spread_markup_pips=_safe_float(account_fields.get("spread_markup_pips")) or 0.0,
            commission_per_lot=_safe_float(account_fields.get("commission_per_lot")) or 0.0,
            commission_pct=_safe_float(account_fields.get("commission_pct")) or 0.0,
            min_spread_pips=_safe_float(min_pips),
            max_spread_pips=_safe_float(max_pips),
            enabled=bool(account_fields.get("enabled", True)),
            source=account_fields.get("source", SOURCE_LEGACY_FALLBACK),
        )
    except Exception as exc:
        logger.debug("[commercial_pricing] build_commercial_pricing_profile failed for %s (non-fatal): %r",
                     symbol, exc)
        return CommercialPricingProfile(
            profile_version=PROFILE_SCHEMA_VERSION, profile_id=SOURCE_CAPTURE_FAILED,
            account_type=None, product_type=None,
            spread_markup_pips=0.0, commission_per_lot=0.0, commission_pct=0.0,
            min_spread_pips=None, max_spread_pips=None, enabled=False, source=SOURCE_LEGACY_FALLBACK,
        )
