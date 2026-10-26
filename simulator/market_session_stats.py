"""Read-only, informational Massive session statistics; never trading inputs.

Separate endpoint and bounded per-process cache. No price-cache, feed,
consumer, account, database or execution writes. Session boundaries belong
to Massive; updated_at is UTC, not a claim that the session starts at UTC.
"""
import math
import os
import threading
import time
from datetime import datetime, timezone

import requests
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from market_data.symbol_specs import allowed_symbols

_TICKERS = {
    'EUR/USD': ('C:EURUSD', 'fx'), 'GBP/USD': ('C:GBPUSD', 'fx'),
    'USD/JPY': ('C:USDJPY', 'fx'), 'AUD/USD': ('C:AUDUSD', 'fx'),
    'BTCUSD': ('X:BTCUSD', 'crypto'), 'ETHUSD': ('X:ETHUSD', 'crypto'),
}
_MAX_AGE = 300
_CACHE = {}
_LOCKS = {symbol: threading.Lock() for symbol in _TICKERS}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _unavailable(symbol):
    return {'symbol': symbol, 'available': False, 'source': 'Massive'}


def _normalize(symbol, payload, now):
    ticker, kind = _TICKERS[symbol]
    if not isinstance(payload, dict) or payload.get('status') != 'OK':
        return _unavailable(symbol)
    rows = payload.get('results')
    if not isinstance(rows, list):
        return _unavailable(symbol)
    row = next((r for r in rows if isinstance(r, dict) and r.get('ticker') == ticker), None)
    if not row or row.get('type') != kind or row.get('error'):
        return _unavailable(symbol)
    session = row.get('session')
    if not isinstance(session, dict):
        return _unavailable(symbol)
    high, low, stamp = (session.get(k) for k in ('high', 'low', 'last_updated'))
    if not (_number(high) and _number(low) and high >= low > 0
            and type(stamp) is int and stamp > 0):
        return _unavailable(symbol)
    updated = stamp / 1_000_000_000  # Massive snapshot timestamps are nanoseconds.
    if not -30 <= now - updated <= _MAX_AGE:
        return _unavailable(symbol)
    volume = session.get('volume')
    if not _number(volume) or volume < 0 or (kind == 'fx' and volume != int(volume)):
        volume = None
    return {
        'symbol': symbol, 'available': True, 'source': 'Massive',
        'period': 'provider_session', 'scope': 'aggregate',
        'high': high, 'low': low, 'volume': volume,
        'volume_unit': 'ticks' if kind == 'fx' else symbol[:-3],
        'updated_at': datetime.fromtimestamp(updated, timezone.utc).isoformat(),
        'expires_at': updated + _MAX_AGE,
    }


def _read_stats(symbol):
    # Bounded to six instruments, with request coalescing per instrument.
    with _LOCKS[symbol]:
        cached = _CACHE.get(symbol)
        if cached and time.monotonic() < cached[0]:
            result = cached[1]
            if result.get('expires_at', float('inf')) < time.time():
                return _unavailable(symbol)
            return result
        result = _unavailable(symbol)
        key = os.environ.get('MASSIVE_API_KEY', '').strip()
        if key:
            try:
                response = requests.get(
                    'https://api.massive.com/v3/snapshot',
                    params={'ticker': _TICKERS[symbol][0]},
                    headers={'Authorization': 'Bearer ' + key},
                    timeout=(3, 5), allow_redirects=False,
                )
                if response.status_code == 200:
                    result = _normalize(symbol, response.json(), time.time())
            except (requests.RequestException, ValueError, OverflowError):
                # No response bodies, secrets or account information in logs.
                pass
        _CACHE[symbol] = (time.monotonic() + (60 if result['available'] else 15), result)
        return result


@require_GET
def market_session_stats(request):
    if not request.user.is_authenticated:
        response = JsonResponse({'available': False}, status=401)
    else:
        symbol = request.GET.get('symbol', '')
        if symbol not in _TICKERS or symbol not in allowed_symbols():
            response = JsonResponse({'available': False}, status=400)
        else:
            response = JsonResponse(_read_stats(symbol))
    response['Cache-Control'] = 'private, no-store'
    return response
