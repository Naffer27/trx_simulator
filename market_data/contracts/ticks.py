"""
market_data/contracts/ticks.py — NormalizedTick contract, schema versioning,
and the tick ordering/idempotency rule.

Defined in FOUNDATION-02 (docs/FOUNDATION_02_MARKET_DATA_CORE.md §1, §8).

Scope of this module (FOUNDATION-03): pure data + pure functions only.
Not imported by feeds.py, consumers.py, symbol_specs.py, or any other
runtime module. No provider is instantiated or contacted here.

What NormalizedTick never carries, by construction — there is no field for
it, this is a structural guarantee, not a runtime check (FOUNDATION-02 §1.3):
broker markup/spread, margin, position size, PnL, challenge/account state,
or any provider's raw wire format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .enums import SourceState

# ─── Schema versioning (FOUNDATION-02 §8) ──────────────────────────────────
#
# schema_version is "MAJOR.MINOR". Same MAJOR = backward compatible (new
# optional fields may have been added; unknown ones must be ignored, not
# rejected). Different MAJOR = a consumer must not read the tick as if it
# were the version it expects — it must degrade safely instead.

CURRENT_SCHEMA_VERSION = "1.0"
_SUPPORTED_MAJOR_VERSIONS = frozenset({1})


def parse_schema_version(version: str) -> tuple[int, int]:
    """Parse "MAJOR.MINOR" into (major, minor). Raises ValueError if malformed."""
    try:
        major_str, minor_str = version.split(".", 1)
        return int(major_str), int(minor_str)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Malformed schema_version: {version!r}") from exc


def is_schema_version_supported(version: str) -> bool:
    """True if this schema major version is one this codebase understands."""
    try:
        major, _minor = parse_schema_version(version)
    except ValueError:
        return False
    return major in _SUPPORTED_MAJOR_VERSIONS


# ─── NormalizedTick ─────────────────────────────────────────────────────────
#
# States considered synthetic for the is_synthetic/source_state coherence
# check (FOUNDATION-02 §3.2): SIMULATION always is; RECOVERY still is, because
# it keeps serving simulation output while probing a real provider in the
# background — a tick isn't real again until the router promotes back to
# LIVE/SECONDARY.
_SYNTHETIC_STATES = frozenset({SourceState.SIMULATION, SourceState.RECOVERY})


@dataclass(frozen=True, kw_only=True)
class NormalizedTick:
    """
    Immutable, provider-agnostic, asset-class-agnostic market data fact.

    Required fields (present on every tick, regardless of asset class):
        schema_version, symbol, provider_id, source_state, is_synthetic,
        is_stale, timestamp_received

    Optional fields (None unless the asset class / provider requires them):
        bid, ask, last, timestamp_provider, sequence, volume,
        open_interest, settlement_price
    """

    # ── required ──
    schema_version: str
    symbol: str
    provider_id: str
    source_state: SourceState
    is_synthetic: bool
    is_stale: bool
    timestamp_received: int

    # ── optional ──
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    timestamp_provider: Optional[int] = None
    sequence: Optional[int] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    settlement_price: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not self.provider_id:
            raise ValueError("provider_id must not be empty")

        state = self.source_state
        if not isinstance(state, SourceState):
            try:
                state = SourceState(state)
            except ValueError:
                raise ValueError(f"Invalid source_state: {self.source_state!r}") from None
            object.__setattr__(self, "source_state", state)

        for field_name in ("bid", "ask", "last"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive when present, got {value!r}")

        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"bid ({self.bid}) must be <= ask ({self.ask})")

        if self.timestamp_received is None:
            raise ValueError("timestamp_received is required")

        if not is_schema_version_supported(self.schema_version):
            raise ValueError(f"Unsupported schema_version: {self.schema_version!r}")

        expected_synthetic = state in _SYNTHETIC_STATES
        if self.is_synthetic != expected_synthetic:
            raise ValueError(
                f"is_synthetic={self.is_synthetic!r} is incoherent with "
                f"source_state={state!r} (expected is_synthetic={expected_synthetic!r})"
            )


# ─── Ordering / idempotency (FOUNDATION-02 §1.6) ───────────────────────────


def should_accept_tick(previous: Optional[NormalizedTick], candidate: NormalizedTick) -> bool:
    """
    Pure ordering/idempotency check for a single (provider_id, symbol) stream.

    `previous` is the last tick this caller accepted for that exact
    (provider_id, symbol) pair, or None if there isn't one yet.

    Monotonicity is enforced only within one (provider_id, symbol) stream —
    never imposed across different providers or different symbols, per
    FOUNDATION-02 §1.6. Comparing ticks from two different streams is a
    caller error this function deliberately does not try to resolve: it
    just accepts, because there is nothing to order against.

    Precedence when both are available: sequence > timestamp_provider >
    timestamp_received (tie-break only).
    """
    if previous is None:
        return True

    if previous.provider_id != candidate.provider_id or previous.symbol != candidate.symbol:
        return True

    if previous.sequence is not None and candidate.sequence is not None:
        return candidate.sequence > previous.sequence

    if previous.timestamp_provider is not None and candidate.timestamp_provider is not None:
        if candidate.timestamp_provider > previous.timestamp_provider:
            return True
        if candidate.timestamp_provider < previous.timestamp_provider:
            return False
        return candidate.timestamp_received > previous.timestamp_received

    return candidate.timestamp_received > previous.timestamp_received
