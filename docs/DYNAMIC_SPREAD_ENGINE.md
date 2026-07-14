# Dynamic Spread Engine (SPREAD-05)

## What this is

`BrokerSpreadConfig.is_dynamic` has existed since the model's original
definition (SPREAD-01-era) but was never read anywhere — confirmed
decorative in the audit that preceded this block. SPREAD-05 is its first
real implementation: an opt-in engine that widens a symbol's effective
spread based on market session, feed source quality, staleness,
volatility, liquidity, and an explicit manual override — instead of the
flat `spread_pips` SPREAD-04 always applied.

`is_dynamic` is the **only** opt-in flag. No second flag was added — every
new field this block introduces (`manual_multiplier`, `manual_reason`,
`manual_expires_at`) only has any effect when `is_dynamic=True`.

## Formula

```
base    = broker_base_spread_pips + account_markup_pips
dynamic = base × session × source × stale × volatility × liquidity × manual

effective_before_bounds = dynamic
effective_after_bounds  = clamp(dynamic, min_spread_pips, max_spread_pips)
```

- `broker_base_spread_pips` — `BrokerSpreadConfig.spread_pips`, unchanged
  meaning from SPREAD-04.
- `account_markup_pips` — the resolved commercial-pricing markup
  (SPREAD-04's `CommercialPricingProfile.spread_markup_pips`), unchanged.
- The raw provider bid/ask already contains the market's own spread —
  this formula never adds it a second time; it only widens the *broker's*
  markup on top.
- Bounds are applied exactly once, at the end, using the same
  `min_spread_pips`/`max_spread_pips` resolution SPREAD-04 established
  (`spread_bounds_enabled` on the symbol's `BrokerSpreadConfig`, or an
  account/product-level override that always wins regardless of that
  flag) — nothing about bounds resolution changed in this block.

All units are pips, round-trip (the same convention `broker_price()` has
always used: the final number is split in half between bid and ask).

## `is_dynamic` as the opt-in

| `is_dynamic` | Behavior |
|---|---|
| `False` (default, every existing and newly-seeded row) | Bit-exact SPREAD-04: `spread_engine.compute_effective_spread_pips()` never even imports `dynamic_spread.py`. No session lookup, no observability read. |
| `True` | The full multiplier chain runs. With every multiplier at its neutral value (session `OPEN`, source `LIVE`, not stale, no volatility/liquidity input, no manual override), the result is still bit-exact to SPREAD-04 — multiplying by `1.0` six times is an exact no-op in IEEE 754. |

This means turning `is_dynamic` on for a symbol is safe by construction:
nothing changes until at least one axis moves away from its neutral
value (the market genuinely leaves `OPEN`, the feed genuinely degrades,
or an admin sets a manual override).

## Multipliers

### Session (`market_data.sessions.evaluate_market_session_for_symbol`)

| State | Multiplier | Reason code |
|---|---|---|
| `OPEN` | 1.00 | `session_open` |
| `PRE_MARKET` | 1.25 | `session_pre_market` |
| `AFTER_HOURS` | 1.35 | `session_after_hours` |
| `CLOSED` / `MAINTENANCE` / `WEEKEND` / `HOLIDAY` | 2.00 | `session_market_closed_wide_spread` |
| `UNKNOWN` / unavailable | 2.00 | `session_unknown_safe_default` / `session_state_unavailable_safe_default` |

This module has no opinion on whether an order is *allowed* right now —
that's `OrderPolicy`'s job (unchanged, untouched by this block). If a
tick somehow arrives while the market is nominally shut or the session
can't be classified, price it defensively rather than at parity.

### Source (`market_data.contracts.SourceState`)

| State | Multiplier | Reason code |
|---|---|---|
| `LIVE` | 1.00 | `source_live` |
| `SECONDARY` | 1.05 | `source_secondary` |
| `RECOVERY` | 1.10 | `source_recovery` |
| `SIMULATION` | 1.15 | `source_simulation` |
| `STALE` | 1.00 (neutral — see below) | `source_stale_handled_by_stale_axis` |
| `MARKET_CLOSED` | 1.00 (neutral — see below) | `source_market_closed_handled_by_session_axis` |
| unrecognized / unavailable | 1.00 | `source_unrecognized_neutral_default` / `source_state_unavailable_safe_default` |

`STALE` and `MARKET_CLOSED` are deliberately kept **neutral** on the
source axis to avoid double-counting: `STALE`'s risk premium is carried
entirely by `stale_multiplier` (below), and `MARKET_CLOSED`'s risk is
already priced by the session axis.

### Staleness

`stale_multiplier = 1.50` when the feed's `source_state == STALE`, else
`1.00`. Reason code `source_stale_wide_spread` when applied.

### Volatility / Liquidity — placeholders, no data provider yet

Both are **optional inputs that default to `None` (neutral, 1.00)**. No
external volatility or liquidity feed exists in this system yet — this
block explicitly defers building one. The formulas exist so the
multiplier chain has a defined shape once a real input arrives:

```
volatility_multiplier = clamp(1.0 + volatility_pips / 100.0, 1.0, 2.0)
liquidity_multiplier  = clamp(1.0 + (1.0 - liquidity_score), 1.0, 2.0)
```

`liquidity_score` is `0.0` (illiquid) .. `1.0` (fully liquid). Both are
bounded to `[1.0, 2.0]` — a formula that can never narrow the spread and
never widen it unboundedly, even given garbage input (invalid input
degrades to `1.00`, never raises).

### Manual override

`BrokerSpreadConfig.manual_multiplier` (`Decimal`, default `1.000`),
`manual_reason` (free text, audit-only), `manual_expires_at` (nullable
`DateTimeField`).

- Only has any effect when `is_dynamic=True`.
- `manual_multiplier <= 0` → treated as invalid, falls back to `1.00`
  (reason `manual_override_invalid`).
- `manual_multiplier == 1.00` → a no-op regardless of expiry (reason
  `manual_override_neutral`).
- If `manual_expires_at` is set and has passed → falls back to `1.00`
  (reason `manual_override_expired`). The stored `manual_multiplier`
  value is **not** cleared or reset — it simply stops applying once
  expired, and resumes if `manual_expires_at` is cleared or pushed
  forward.
- `manual_reason` is folded into the reason code
  (`manual_override_active:<reason>`) so the audit trail carries the
  human explanation, but never affects pricing math.

## Bounds

Unchanged from SPREAD-04's opt-in correction: `spread_bounds_enabled`
gates whether the symbol's own `min_spread`/`max_spread` are read at all;
an account/product-level override always wins regardless of that flag.
The dynamic engine applies bounds to the **post-multiplier** value
(`effective_before_bounds`), not to the raw base+markup — a symbol with a
5-pip static base and a 2x session multiplier that would produce 10 pips
still gets clamped to its configured ceiling, same as any other value.

## Audit snapshot (`pricing_context.py`, schema v4)

Every tick, `consumers.py::price_tick()` resolves the dynamic decision
**once** (`dynamic_spread.build_dynamic_inputs()` → frozen
`DynamicSpreadInputs`) and reuses that same frozen object for both:

1. `broker_price()` — to price the actual fill.
2. `pricing_context.tick_pricing_snapshot()` — to freeze the audit
   record.

Because `evaluate_dynamic_spread()` is a pure function of that frozen
object, both calls are bit-identical without threading a computed
decision across the two call sites — the same "freeze inputs, not
outputs" pattern SPREAD-02b established for the static path.

New fields on `PricingContext` (all `None` on any row where the dynamic
engine never ran — including every row captured before this block):

- `dynamic_spread_enabled`
- `session_multiplier`, `source_multiplier`, `stale_multiplier`,
  `volatility_multiplier`, `liquidity_multiplier`, `manual_multiplier`
- `reason_codes` — the full list of why each multiplier landed where it
  did
- `decision_id` — deterministic hash of every input plus the final
  number; identical inputs always produce the identical id, so two
  independent evaluations of the same tick (pricing vs. audit) can be
  verified to agree.

`effective_spread_pips_pre_clamp`/`effective_spread_pips` (existing
SPREAD-04 fields) now generalize to mean "after the multiplier chain,
before bounds" / "after bounds" — identical to their SPREAD-04 meaning
whenever `dynamic_spread_enabled` is `False` or every multiplier is
neutral.

## Rollback

Set `BrokerSpreadConfig.is_dynamic = False` for the affected symbol (or
all symbols). Effective immediately on the next cache refresh (≤30s,
`spread_config_cache.REFRESH_INTERVAL_SECONDS`) — no code change, no
migration, no restart required. Every multiplier reverts to being
uncomputed (not just neutral) and pricing returns to the exact SPREAD-04
formula.

## Seed command

```
python manage.py seed_broker_spread_configs [--force-update]
```

Idempotently creates one `BrokerSpreadConfig` row per
`market_data.symbol_specs.allowed_symbols()` (currently 6: `EUR/USD`,
`GBP/USD`, `USD/JPY`, `AUD/USD`, `BTCUSD`, `ETHUSD`). `spread_pips` is
derived from each `SymbolSpec`'s own `spread`/`pip_size`. Every seeded row
is inert: `is_dynamic=False`, `spread_bounds_enabled=False`,
`manual_multiplier=1.000`. Without `--force-update`, an existing row
(including its `is_dynamic`/bounds/manual fields — operator decisions,
never seed data) is left completely untouched; with it, only
`spread_pips` is refreshed.

## What's still pending

- No real volatility or liquidity data provider — both inputs are wired
  end-to-end but always default to `None`/neutral until one exists.
- No admin UI convenience for setting a time-boxed manual override beyond
  the raw `manual_multiplier`/`manual_reason`/`manual_expires_at` fields
  already exposed in Django admin.
- The dashboard is untouched — clients still see only the final
  bid/ask; none of the new audit fields are surfaced client-side.
