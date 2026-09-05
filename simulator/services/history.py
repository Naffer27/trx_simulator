# simulator/services/history.py
"""
FIX-HISTORY-AUTO-CLOSE-SYNC-01 — single, shared definition of "closed
trades for History".

Extracted verbatim from simulator/views.py's dashboard seed (the query/
serialization shape was already correct — see the design lock's own
root-cause audit) so simulator/consumers.py's `get_closed_trades` WS
action can reuse the exact same source of truth without importing
views.py (an HTTP-layer module) from a Channels consumer, and without a
second, independently-drifting definition of what "closed trades" means.

Pure, synchronous, DB-touching. A caller from an async context
(consumers.py) MUST wrap calls to closed_trades_for_account() in
database_sync_to_async — exactly like every other ORM access already
does in that consumer. Never call this directly from an `async def`
without that wrapper.
"""
from ..models import Trade

CLOSED_TRADES_DEFAULT_LIMIT = 50


def closed_trades_for_account(account_or_account_id, limit: int = CLOSED_TRADES_DEFAULT_LIMIT) -> list:
    """
    Returns a list of plain dicts already in the exact shape both the
    server-rendered seed (views.py) and the live WS path (consumers.py's
    order_close handler / dashboard.html's closedTradesHistory) use:
    {id, symbol, side, qty, entry, close, pnl, ts} — ts in epoch
    milliseconds, derived from Trade.closed_at (the real, authoritative
    close time — never a synthetic/receipt-time value).

    Same filter/order/limit as the original views.py query: this
    account only, closed_at IS NOT NULL, newest first, capped at
    `limit` (default 50). No joins, no close_reason (not a Trade
    field), no new persistence, no migration.

    `account_or_account_id` may be a TradingAccount instance or a bare
    pk — Django's ORM resolves an FK filter identically either way.
    """
    qs = (
        Trade.objects
        .filter(account=account_or_account_id, closed_at__isnull=False)
        .order_by('-closed_at')
        .values('id', 'symbol', 'trade_type', 'lot_size',
                'entry_price', 'exit_price', 'profit_loss', 'closed_at')
        [:limit]
    )
    return [
        {
            'id':     t['id'],
            'symbol': t['symbol'],
            'side':   t['trade_type'].lower(),
            'qty':    float(t['lot_size']),
            'entry':  float(t['entry_price']),
            'close':  float(t['exit_price']) if t['exit_price'] is not None else None,
            'pnl':    float(t['profit_loss']) if t['profit_loss'] is not None else None,
            'ts':     int(t['closed_at'].timestamp() * 1000),
        }
        for t in qs
    ]
