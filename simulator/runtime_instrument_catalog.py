"""
simulator/runtime_instrument_catalog.py — FOUNDATION-12 drift-check
orchestration.

market_data/catalog/ stays Django-free (matching every other market_data/*
package's isolation guarantee), so the piece that actually needs
simulator.models.Instrument lives here instead — this module is the only
thing in the codebase allowed to bridge "the new Runtime Instrument
Catalog facade" and "the DB catalog" together.

Gated by settings.MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED (default False).
Not wired into FeedManager or any live request path in this block — no
loop, no provider, no trading rule, no price, no payload changes. This
exists so the flag has something real and tested to gate, ready for a
future Foundation to call in-band without inventing new plumbing then.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from market_data.catalog import compare_runtime_instrument
from market_data.instruments import (
    DriftReport,
    profile_from_instrument,
    provider_mappings_for_instrument,
)
from market_data.symbol_specs import normalize_symbol

from .models import Instrument

log = logging.getLogger("simulator.ws")


def runtime_catalog_drift_check_enabled() -> bool:
    return bool(getattr(settings, "MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED", False))


def _find_instrument_row(canonical_symbol: str) -> Optional[Instrument]:
    compact_symbol = canonical_symbol.replace("/", "").upper()
    return Instrument.objects.filter(symbol=compact_symbol).first()


def check_runtime_catalog_drift(canonical_symbol: str) -> Optional[DriftReport]:
    """
    If the flag is off, returns None immediately — zero DB access, zero
    behavior. If on: looks up the matching Instrument row (if any),
    compares it against get_runtime_instrument(canonical_symbol), logs any
    drift, and returns the report. Never raises — this is observability
    tooling, not something that may ever affect the caller.
    """
    if not runtime_catalog_drift_check_enabled():
        return None

    try:
        instrument = _find_instrument_row(normalize_symbol(canonical_symbol))
        if instrument is None:
            return None

        mappings = provider_mappings_for_instrument(instrument)
        alternate_profile = profile_from_instrument(instrument, provider_mappings=mappings)
        report = compare_runtime_instrument(canonical_symbol, alternate_profile)

        if report.critical_differences:
            log.warning(
                "event=market_data_runtime_catalog_drift symbol=%s critical=%d warning=%d fields=%s",
                canonical_symbol, len(report.critical_differences), len(report.warning_differences),
                sorted(d.field for d in report.critical_differences),
            )
        elif report.warning_differences:
            log.info(
                "event=market_data_runtime_catalog_drift symbol=%s critical=%d warning=%d fields=%s",
                canonical_symbol, 0, len(report.warning_differences),
                sorted(d.field for d in report.warning_differences),
            )
        return report
    except Exception as exc:
        log.debug(
            "[catalog] drift check failed for %s (non-fatal): %r", canonical_symbol, exc,
        )
        return None
