"""
simulator/pnl_engine.py — MARGIN-02: Quote Currency PnL Conversion.

Root cause fixed: every PnL formula in this codebase (WS unrealized/
realized, daemon offline equity, daemon Step 3/5 closes) computed
`(close - entry) * qty * contract_size` and treated the result as if it
were already in the account's currency. That is only true when the
instrument's quote currency equals the account currency. For a pair like
USD/JPY (base=USD, quote=JPY), the raw formula produces a number
denominated in JPY — adding it straight to a USD balance overstates (or
understates) PnL by roughly the USD/JPY exchange rate (~155x today).
USD/JPY is enabled and tradeable, so this was a live, exploitable bug,
not a theoretical one.

This module is the single, pure source of truth for "what is this
position's PnL, in the account's currency". Every real PnL path
(unrealized WS, unrealized daemon, manual close, TP, SL, stop-out,
liquidation, Trade.profit_loss) must go through calculate_position_pnl()
— never re-implement the formula inline.

Two-stage pipeline:
  1. calculate_quote_pnl() — the existing, unchanged formula, in the
     instrument's own quote currency. This is NOT the bug; the formula
     itself is correct for what it computes. It just was never converted.
  2. convert_pnl_to_account_currency() — the new step. Pure, explicit
     conversion rules, never fabricates a rate:
       a. quote_currency == account_currency -> no conversion needed.
       b. base_currency == account_currency -> divide by the instrument's
          own close price (the instrument's price IS the exchange rate
          between base and quote — no separate FX feed required). This is
          exactly the USD/JPY case: base=USD=account_currency, so
          pnl_account = pnl_quote / close_price.
       c. Anything else (a genuine cross that needs a third-currency
          rate, or a non-USD account) — only converts if the caller
          supplies an explicit, already-fetched rate in conversion_prices;
          otherwise returns a structured, non-fabricated error
          (converted=False, error_code set, pnl_account=None). No
          instrument is currently affected by this branch — every
          enabled symbol is either quote=USD or (for USD/JPY) base=USD,
          and every real TradingAccount.currency in this system is 'USD'
          (verified: 33/33 rows, and no creation path ever sets it to
          anything else — see MARGIN-01/MARGIN-02 audit).

base_currency/quote_currency are read from SymbolSpec via
market_data.instruments.bridges.profile_from_symbol_spec() — the
existing, pure, DB-free bridge (FOUNDATION-06). Deliberately NOT the DB
Instrument model: MARGIN-01 confirmed Instrument.pnl_mode/max_leverage
are already-diagnosed-but-unwired decorative fields, and per this
block's explicit rule, the DB model must not become a runtime dependency
per tick.

Never raises. Every function here degrades to a safe, clearly-labeled
result — a PnL calculation must never crash a tick, a close, or a daemon
cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger("simulator.pnl")

SCHEMA_VERSION = 1

CONVERSION_MODE_NONE                 = "no_conversion"           # quote == account currency
CONVERSION_MODE_BASE_ACCOUNT_INVERSE = "base_account_inverse"    # base == account currency
CONVERSION_MODE_EXPLICIT_RATE        = "explicit_rate"           # caller-supplied conversion_prices
CONVERSION_MODE_UNSUPPORTED          = "unsupported"              # no rate available — safe failure

ERROR_NO_CONVERSION_RATE = "no_conversion_rate_available"
ERROR_INVALID_INPUT      = "invalid_pnl_input"


@dataclass(frozen=True)
class PositionPnLResult:
    pnl_quote: Decimal
    quote_currency: str
    pnl_account: Optional[Decimal]
    account_currency: str
    conversion_mode: str
    conversion_rate: Optional[Decimal]
    conversion_symbol: Optional[str]
    converted: bool
    error_code: Optional[str]

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "pnl_quote": float(self.pnl_quote),
            "quote_currency": self.quote_currency,
            "pnl_account": float(self.pnl_account) if self.pnl_account is not None else None,
            "account_currency": self.account_currency,
            "conversion_mode": self.conversion_mode,
            "conversion_rate": float(self.conversion_rate) if self.conversion_rate is not None else None,
            "conversion_symbol": self.conversion_symbol,
            "converted": self.converted,
            "error_code": self.error_code,
        }


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def calculate_quote_pnl(
    side: str,
    entry_price,
    close_price,
    quantity,
    contract_size,
) -> Decimal:
    """
    PnL in the instrument's own quote currency. Unchanged formula from
    before this block — this was never the bug, only what happened to
    its result afterward. side is case-insensitive ('buy'/'BUY'/'sell'/'SELL').

    Never raises: invalid numeric input returns Decimal('0').
    """
    try:
        entry = _to_decimal(entry_price)
        close = _to_decimal(close_price)
        qty = _to_decimal(quantity)
        cs = _to_decimal(contract_size)
        is_buy = str(side).strip().lower() == "buy"
        diff = (close - entry) if is_buy else (entry - close)
        return diff * qty * cs
    except (InvalidOperation, TypeError, ValueError) as exc:
        logger.warning("[pnl_engine] calculate_quote_pnl failed (non-fatal): %r", exc)
        return Decimal("0")


def convert_pnl_to_account_currency(
    pnl_quote,
    base_currency: str,
    quote_currency: str,
    account_currency: str,
    conversion_prices: Optional[dict] = None,
    conversion_symbol: Optional[str] = None,
) -> PositionPnLResult:
    """
    Converts pnl_quote (already computed by calculate_quote_pnl) into
    account_currency. Never fabricates a rate — see module docstring for
    the three conversion modes and the explicit failure case.

    conversion_prices, when given, is a dict of {symbol_or_pair: price}
    that MAY contain the instrument's own close price under
    conversion_symbol (used for the base==account_currency inverse case)
    and/or an explicit third-currency rate for the unsupported-cross case
    (keyed "{quote_currency}/{account_currency}" or the reverse) — never
    invented here, only consulted.
    """
    try:
        pnl_q = _to_decimal(pnl_quote)
    except (InvalidOperation, TypeError, ValueError) as exc:
        logger.warning("[pnl_engine] invalid pnl_quote (non-fatal): %r", exc)
        return PositionPnLResult(
            pnl_quote=Decimal("0"), quote_currency=quote_currency,
            pnl_account=None, account_currency=account_currency,
            conversion_mode=CONVERSION_MODE_UNSUPPORTED, conversion_rate=None,
            conversion_symbol=conversion_symbol, converted=False,
            error_code=ERROR_INVALID_INPUT,
        )

    if quote_currency == account_currency:
        return PositionPnLResult(
            pnl_quote=pnl_q, quote_currency=quote_currency,
            pnl_account=pnl_q, account_currency=account_currency,
            conversion_mode=CONVERSION_MODE_NONE, conversion_rate=Decimal("1"),
            conversion_symbol=None, converted=True, error_code=None,
        )

    prices = conversion_prices or {}

    if base_currency == account_currency:
        rate = prices.get(conversion_symbol) if conversion_symbol else None
        if rate is None:
            logger.warning(
                "[pnl_engine] base_account_inverse requires the instrument's own "
                "close price under conversion_symbol=%r, none supplied — safe failure",
                conversion_symbol,
            )
            return PositionPnLResult(
                pnl_quote=pnl_q, quote_currency=quote_currency,
                pnl_account=None, account_currency=account_currency,
                conversion_mode=CONVERSION_MODE_UNSUPPORTED, conversion_rate=None,
                conversion_symbol=conversion_symbol, converted=False,
                error_code=ERROR_NO_CONVERSION_RATE,
            )
        try:
            rate_d = _to_decimal(rate)
            if rate_d == 0:
                raise InvalidOperation("zero rate")
            pnl_account = pnl_q / rate_d
        except (InvalidOperation, TypeError, ValueError) as exc:
            logger.warning("[pnl_engine] invalid conversion rate %r (non-fatal): %r", rate, exc)
            return PositionPnLResult(
                pnl_quote=pnl_q, quote_currency=quote_currency,
                pnl_account=None, account_currency=account_currency,
                conversion_mode=CONVERSION_MODE_UNSUPPORTED, conversion_rate=None,
                conversion_symbol=conversion_symbol, converted=False,
                error_code=ERROR_NO_CONVERSION_RATE,
            )
        return PositionPnLResult(
            pnl_quote=pnl_q, quote_currency=quote_currency,
            pnl_account=pnl_account, account_currency=account_currency,
            conversion_mode=CONVERSION_MODE_BASE_ACCOUNT_INVERSE, conversion_rate=rate_d,
            conversion_symbol=conversion_symbol, converted=True, error_code=None,
        )

    # Neither quote nor base matches the account currency — a genuine
    # cross. Only proceed with an explicit, caller-supplied rate; never
    # invent one. No instrument in this system exercises this branch
    # today (see module docstring) — this is the "no soporte falso"
    # guarantee for whenever one does.
    for pair in (f"{quote_currency}/{account_currency}", f"{account_currency}/{quote_currency}"):
        if pair in prices:
            try:
                rate_d = _to_decimal(prices[pair])
                if rate_d == 0:
                    raise InvalidOperation("zero rate")
            except (InvalidOperation, TypeError, ValueError):
                continue
            pnl_account = pnl_q * rate_d if pair.startswith(quote_currency) else pnl_q / rate_d
            return PositionPnLResult(
                pnl_quote=pnl_q, quote_currency=quote_currency,
                pnl_account=pnl_account, account_currency=account_currency,
                conversion_mode=CONVERSION_MODE_EXPLICIT_RATE, conversion_rate=rate_d,
                conversion_symbol=pair, converted=True, error_code=None,
            )

    logger.error(
        "[pnl_engine] event=unsupported_currency_conversion base=%s quote=%s "
        "account_currency=%s — no explicit rate available, refusing to fabricate one",
        base_currency, quote_currency, account_currency,
    )
    return PositionPnLResult(
        pnl_quote=pnl_q, quote_currency=quote_currency,
        pnl_account=None, account_currency=account_currency,
        conversion_mode=CONVERSION_MODE_UNSUPPORTED, conversion_rate=None,
        conversion_symbol=None, converted=False,
        error_code=ERROR_NO_CONVERSION_RATE,
    )


def calculate_position_pnl(
    side: str,
    entry_price,
    close_price,
    quantity,
    symbol: str,
    account_currency: str = "USD",
) -> PositionPnLResult:
    """
    The single entry point every real PnL path should call. Pure, DB-free:
    reads only market_data.symbol_specs (an in-memory registry) via
    market_data.instruments.bridges.profile_from_symbol_spec() — never
    the DB Instrument model, never a network call, zero ORM. Safe to call
    per tick.

    The instrument's own close_price doubles as the conversion rate for
    the base_account_inverse case (USD/JPY: the JPY-per-USD rate IS the
    price being quoted) — no separate FX feed needed for that case.

    Never raises — any internal failure (unknown symbol, garbage input)
    degrades to a PositionPnLResult with converted=False and an explicit
    error_code, never a fabricated number.
    """
    try:
        from market_data.symbol_specs import get_spec, normalize_symbol
        from market_data.instruments.bridges import profile_from_symbol_spec

        canonical = normalize_symbol(symbol)
        spec = get_spec(canonical)
        profile = profile_from_symbol_spec(spec)

        pnl_quote = calculate_quote_pnl(side, entry_price, close_price, quantity, spec.contract_size)

        return convert_pnl_to_account_currency(
            pnl_quote, profile.base_currency, profile.quote_currency, account_currency,
            conversion_prices={canonical: close_price},
            conversion_symbol=canonical,
        )
    except Exception as exc:
        logger.error("[pnl_engine] calculate_position_pnl failed for %s (non-fatal): %r", symbol, exc)
        return PositionPnLResult(
            pnl_quote=Decimal("0"), quote_currency="",
            pnl_account=None, account_currency=account_currency,
            conversion_mode=CONVERSION_MODE_UNSUPPORTED, conversion_rate=None,
            conversion_symbol=None, converted=False,
            error_code=ERROR_INVALID_INPUT,
        )


def position_pnl_float(
    side: str,
    entry_price,
    close_price,
    quantity,
    symbol: str,
    account_currency: str = "USD",
) -> float:
    """
    Thin convenience wrapper around calculate_position_pnl() returning a
    plain float in account_currency — the shape every existing call site
    in consumers.py/tasks.py already expects (both operate on floats
    end-to-end; this block does not migrate that to Decimal). This is the
    ONE function both the WS runtime and the Celery daemon call, so
    neither path can compute a different number for the same inputs.

    If conversion is unsupported (converted=False — unreachable today,
    see module docstring), logs a CRITICAL error and returns 0.0 rather
    than crash a tick or fabricate a number. 0.0 is a deliberate,
    loudly-logged fallback, not a silent guess — a future block adding a
    non-USD account or an unsupported cross must decide the real UX
    (reject the position, halt the account, etc.); this block's scope is
    the currently-live USD/JPY bug, which this branch never reaches.
    """
    result = calculate_position_pnl(side, entry_price, close_price, quantity, symbol, account_currency)
    if not result.converted or result.pnl_account is None:
        logger.critical(
            "[pnl_engine] event=pnl_conversion_unsupported symbol=%s account_currency=%s "
            "error_code=%s — returning 0.0, NOT a fabricated number. This should be "
            "unreachable with current account currencies/enabled symbols.",
            symbol, account_currency, result.error_code,
        )
        return 0.0
    return float(result.pnl_account)
