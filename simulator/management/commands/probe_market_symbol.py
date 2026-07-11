"""
Read-only diagnostic: probe a live REST quote from Finnhub for a symbol.

Does NOT write to the DB, does NOT change settings, does NOT enable any
instrument, does NOT touch market_data/feeds.py. It only performs the same
kind of REST call feeds.py._fetch_rest_price() already makes for Finnhub-
routed symbols (forex, metals) — useful to check gold/forex data readiness
before flipping SymbolSpec.enabled.

Usage:
    python manage.py probe_market_symbol XAU/USD
    python manage.py probe_market_symbol EUR/USD
    python manage.py probe_market_symbol OANDA:XAU_USD --raw
    python manage.py probe_market_symbol FX:EURUSD --raw --timeout 8

By default SYMBOL is looked up in market_data/symbol_specs.py and its
finnhub_symbol is used. Pass --raw to probe a literal provider symbol
directly (e.g. copy-pasted from Finnhub docs), bypassing the registry.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from market_data.symbol_specs import get_spec

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"


def probe_finnhub_quote(provider_symbol: str, api_key: str, timeout: float = 5.0) -> dict:
    """
    Pure, read-only Finnhub /quote probe. No DB access, no settings mutation.

    Returns either:
      {"ok": True,  "price": float, "open": float|None, "high": float|None,
       "low": float|None, "prev_close": float|None, "timestamp": int|None}
    or:
      {"ok": False, "error": str}
    """
    if not api_key:
        return {"ok": False, "error": "missing_api_key: FINNHUB_API_KEY no está configurada (settings/env)."}

    if not provider_symbol:
        return {"ok": False, "error": "missing_provider_symbol"}

    url = (
        f"{FINNHUB_QUOTE_URL}?symbol={urllib.parse.quote(provider_symbol)}"
        f"&token={urllib.parse.quote(api_key)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "trx-sim/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except TimeoutError as exc:
        return {"ok": False, "error": f"timeout: {exc}"}
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return {"ok": False, "error": f"timeout: {exc.reason}"}
        return {"ok": False, "error": f"network_error: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive catch-all for a diagnostic CLI
        return {"ok": False, "error": f"network_error: {exc!r}"}

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid_json: {exc}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": f"invalid_response: expected object, got {type(data).__name__}"}

    price = data.get("c")
    try:
        price = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        price = 0.0

    if price <= 0:
        return {"ok": False, "error": f"invalid_response: no current price in payload ({data!r})"}

    def _f(key):
        v = data.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v else None

    return {
        "ok": True,
        "price": price,
        "open": _f("o"),
        "high": _f("h"),
        "low": _f("l"),
        "prev_close": _f("pc"),
        "timestamp": int(data["t"]) if data.get("t") else None,
    }


class Command(BaseCommand):
    help = (
        "Read-only diagnostic: probe a live Finnhub quote for a registry symbol "
        "(or a raw provider symbol with --raw). Does not write to the DB, does "
        "not change settings, does not enable any instrument."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "symbol",
            help="Registry symbol (e.g. XAU/USD, EUR/USD) or, with --raw, a literal "
                 "Finnhub provider symbol (e.g. OANDA:XAU_USD, FX:EURUSD).",
        )
        parser.add_argument(
            "--raw", action="store_true",
            help="Treat SYMBOL as a literal Finnhub provider symbol, skipping the registry lookup.",
        )
        parser.add_argument(
            "--timeout", type=float, default=5.0,
            help="HTTP timeout in seconds (default: 5.0).",
        )

    def handle(self, *args, **options):
        symbol  = options["symbol"]
        raw     = options["raw"]
        timeout = options["timeout"]

        if raw:
            registry_symbol = None
            provider_symbol = symbol
        else:
            try:
                spec = get_spec(symbol)
            except KeyError:
                self.stderr.write(self.style.ERROR(
                    f"Status          : FAIL\n"
                    f"Error           : unknown_symbol — {symbol!r} no está en "
                    f"market_data/symbol_specs.py. Usa --raw para un símbolo de "
                    f"proveedor directo (p.ej. OANDA:XAU_USD)."
                ))
                return
            if not spec.finnhub_symbol:
                self.stderr.write(self.style.ERROR(
                    f"Status          : FAIL\n"
                    f"Error           : no_finnhub_symbol — {symbol!r} no tiene "
                    f"finnhub_symbol configurado en symbol_specs.py."
                ))
                return
            registry_symbol = symbol
            provider_symbol = spec.finnhub_symbol

        api_key = (getattr(settings, "FINNHUB_API_KEY", "") or os.getenv("FINNHUB_API_KEY", "") or "").strip()

        self.stdout.write(f"Registry symbol : {registry_symbol or '(raw — no registry lookup)'}")
        self.stdout.write(f"Provider symbol : {provider_symbol}")

        result = probe_finnhub_quote(provider_symbol, api_key, timeout=timeout)

        if not result["ok"]:
            self.stdout.write(self.style.ERROR("Status          : FAIL"))
            self.stdout.write(self.style.ERROR(f"Error           : {result['error']}"))
            return

        self.stdout.write(self.style.SUCCESS("Status          : OK"))
        self.stdout.write(f"Price (current) : {result['price']}")
        self.stdout.write(f"Open            : {result['open']}")
        self.stdout.write(f"High            : {result['high']}")
        self.stdout.write(f"Low             : {result['low']}")
        self.stdout.write(f"Prev close      : {result['prev_close']}")
        self.stdout.write(f"Timestamp (unix): {result['timestamp']}")
        self.stdout.write(
            "\nNote: Finnhub's free /quote endpoint does not return separate "
            "bid/ask — feeds.py derives bid/ask synthetically from this price "
            "using the instrument's configured spread."
        )
