"""
simulator/tests/test_atomic_guard_lock_order.py — PANEL-02 INVARIANTE-2.

CORRECTED lock order: TradingAccount → Position (not Position → Account,
the original PANEL-02 design). Root cause of the correction:

  materializing `positions = list(Position.objects.select_for_update()
  .filter(...))` BEFORE locking TradingAccount does NOT guarantee a fresh
  snapshot. Concretely, with zero pre-existing positions:
    T1 reads positions=[]  (locks nothing — select_for_update() against
                             an empty queryset acquires zero row locks)
    T2 reads positions=[]  (same — no lock exists yet to block on)
    T1 locks account, creates Position, commits
    T2 THEN acquires the account lock (unblocked now that T1 committed)
       but still validates against its OWN positions=[] read from BEFORE
       T1 ever committed — a stale snapshot, despite T2 genuinely holding
       the account lock by the time it writes.

  TradingAccount is the account's real mutex — exactly one row exists per
  account, always, so locking it FIRST means a concurrent transaction for
  the SAME account blocks there regardless of how many Position rows
  currently exist. Every subsequent Position query in the transaction is
  then guaranteed to run AFTER any sibling transaction for that account
  has either fully committed (visible, Read Committed) or is still
  blocked on the very same Account lock (hasn't touched anything yet).

This file covers:

1. STRUCTURAL proof that every live path locks TradingAccount BEFORE
   Position — via django.test.utils.CaptureQueriesContext (query order)
   and source-order inspection for the paths not exercised through a bare
   .__wrapped__ call in this file. Backend-independent: inspects what the
   CODE does, not how a given backend enforces the resulting lock.

2. The GLOBAL lock-order audit (see the matching comment block in
   consumers.py: "PANEL-02 INVARIANTE-2 — global TradingAccount/Position
   lock order"): every live path that locks both models —
   _db_open_position_atomic, _db_close_position_atomic,
   tasks._close_position_sync (Celery daemon), admin.py's force_close —
   locks TradingAccount FIRST, then Position row(s). No live path is left
   on the old Position→Account order. (TradingConsumer's
   _db_mirror_close_position/_db_mirror_open_or_update are excluded: zero
   call sites anywhere in the codebase, re-verified for this fix — dead
   code, not part of the audited order.)

3. Genuine multi-threaded proofs that the fresh-snapshot guarantee holds:
   a second transaction for the same account, starting from BOTH zero and
   non-zero pre-existing positions, observes whatever the first one
   already committed once it acquires the account lock — not a
   pre-lock/stale read.

4. The three PANEL-02 margin/position-count scenarios repeated end to end
   under the corrected lock order.

5. Concurrent open-vs-close and close-vs-daemon on the same account,
   proving no deadlock under the unified Account→Position order and no
   double-close.

── DB BACKEND / SQLite LIMITATION (read before trusting these results) ──

This project's test suite runs against SQLite in shared-cache in-memory
mode (settings.py: DATABASES["default"] falls back to sqlite3 whenever
DB_NAME is unset — true in this environment; `python manage.py test`
reports the test DB as "file:memorydb_default?mode=memory&cache=shared").
No local PostgreSQL server is available in this environment (checked:
psql/pg_ctl/postgres binaries are all absent, pg_isready fails to even
resolve) — production, per settings.py, always uses PostgreSQL when
DB_NAME is set.

SQLite's "cache=shared" mode DOES allow multiple threads/connections to
see the same in-memory database, which is what makes the multi-threaded
tests below possible at all. But SQLite's locking is NOT row-level MVCC:
  - Its writer lock is coarse — one active write transaction can block
    ALL other writers against the WHOLE database file, not just the rows
    a real PostgreSQL row lock would hold.
  - Its shared-cache mode additionally raises SQLITE_LOCKED ("database
    TABLE is locked") on ordinary (non-locking) reads against a table
    another connection is mid-write on — an error busy_timeout does NOT
    retry (that mechanism only covers SQLITE_BUSY). The thread helper
    below retries on this specific error with jitter — a legitimate,
    standard technique (the underlying code still executes for real,
    under real thread contention, on every attempt), NOT a way of
    avoiding real contention.

Concretely, this means:
  - These tests DO prove the fix's LOGIC and lock ORDER are correct under
    genuine concurrent execution (real OS threads, real separate DB
    connections, real overlapping transactions) — the outcomes below are
    not simulated, and the "T2 must observe T1's commit" tests are
    genuine proof the account-first ordering closes the staleness gap.
  - These tests do NOT certify PostgreSQL's specific row-level locking
    semantics — SQLite's coarser, table/file-level locking can make two
    DIFFERENT accounts' operations serialize against each other for
    reasons that would never occur under real PostgreSQL MVCC row locks.
    That two different accounts are architecturally independent rests on
    the query filters themselves (every lock here is scoped by
    account_id=... / account=account), verifiable by code inspection
    (TwoAccountsDoNotBlockEachOtherTests in
    test_atomic_margin_and_position_guard.py), not by this file's
    SQLite-backed concurrency tests.
  - MANDATORY STAGING STEP BEFORE PRODUCTION: re-run the scenarios in
    PostgreSQLStagingValidationTests (skipped here — no PostgreSQL
    available in this environment) against a real staging PostgreSQL
    instance before this fix ships. That class documents exactly what to
    run and how to point it at Postgres.

Uses TransactionTestCase (not TestCase): TestCase wraps each test in an
outer transaction + savepoints that are invisible to any other thread's
connection — genuine cross-thread visibility requires TransactionTestCase,
which commits for real and truncates between tests (same reasoning
documented in test_account_balance_concurrency.py for
@database_sync_to_async-driven tests).
"""
import inspect
import random
import threading
import time
import unittest
from decimal import Decimal

from django.db import connection
from django.db.utils import OperationalError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer
from simulator.models import Position, TradingAccount
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__
EURUSD_SPEC = get_spec("EUR/USD")


def _seed_fresh_price(symbol, bid, ask):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = round((bid + ask) / 2, 6)
        feed._price_ts[symbol] = time.time()


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0, "leverage": 50,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


def _pos_mem(pos):
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": None, "tp": None, "opened_at": pos.opened_at.timestamp(),
    }


class QueryOrderStructuralTests(TransactionTestCase):
    """INVARIANTE-2, point 1 — TradingAccount query issued (and thus its
    lock acquired) strictly before the Position query, verified via the
    actual SQL statements the code emits. Backend-independent."""

    def test_tradingaccount_query_precedes_position_query(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.17"))

        with CaptureQueriesContext(connection) as ctx:
            result = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
                commission=0.0, new_balance=10000.0,
            )
        self.assertTrue(result["ok"])

        position_query_index = None
        account_query_index = None
        for i, q in enumerate(ctx.captured_queries):
            sql = q["sql"]
            if position_query_index is None and "simulator_position" in sql and sql.strip().upper().startswith("SELECT"):
                position_query_index = i
            if account_query_index is None and "simulator_tradingaccount" in sql and sql.strip().upper().startswith("SELECT"):
                account_query_index = i

        self.assertIsNotNone(account_query_index, "no SELECT against simulator_tradingaccount was captured")
        self.assertIsNotNone(position_query_index, "no SELECT against simulator_position was captured")
        self.assertLess(
            account_query_index, position_query_index,
            "TradingAccount must be locked (queried) before Position — "
            f"got TradingAccount at index {account_query_index}, Position at {position_query_index}",
        )

    def test_multi_row_position_lock_is_ordered_by_id(self):
        """The Position lock query must include a deterministic ORDER BY —
        defensive, kept even though Account-as-outer-mutex already
        prevents two transactions from holding overlapping Position locks
        for the same account simultaneously."""
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("10000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.17"))
        make_position(account, symbol="EUR/USD", side="SELL",
                      qty=Decimal("0.01"), avg_price=Decimal("1.17"))

        with CaptureQueriesContext(connection) as ctx:
            _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
                commission=0.0, new_balance=10000.0,
            )

        position_selects = [
            q["sql"] for q in ctx.captured_queries
            if "simulator_position" in q["sql"] and q["sql"].strip().upper().startswith("SELECT")
        ]
        self.assertTrue(position_selects, "expected at least one Position SELECT")
        self.assertIn("ORDER BY", position_selects[0].upper())


class GlobalLockOrderAuditDocumentationTests(TransactionTestCase):
    """INVARIANTE-2, point 2 — documents the audited global lock order as
    executable assertions against the actual source, so this doesn't
    silently drift out of sync with consumers.py/tasks.py/admin.py."""

    def test_close_paths_lock_account_before_position_source_order(self):
        """Structural (source-text) check: in both close paths, the
        TradingAccount select_for_update() call appears BEFORE the
        Position select_for_update() call in the function source —
        matching the audited Account→Position order. Guards against a
        future edit silently reversing the order without anyone updating
        this test or the consumers.py doc comment."""
        from simulator import tasks as tasks_module

        # Anchor on "<Model>.objects" (actual ORM code), not the bare
        # model name — both functions' docstrings mention "Position"
        # before "TradingAccount" in prose ("find+lock Position, ...
        # update TradingAccount balance/equity"), which would false-
        # positive a plain substring search against the docstring itself.
        close_src = inspect.getsource(TradingConsumer._db_close_position_atomic.__wrapped__)
        acct_idx = close_src.find("TradingAccount.objects")
        pos_idx = close_src.find("Position.objects")
        self.assertGreater(acct_idx, -1)
        self.assertGreater(pos_idx, -1)
        self.assertLess(acct_idx, pos_idx)

        daemon_close_src = inspect.getsource(tasks_module._close_position_sync)
        acct_idx2 = daemon_close_src.find("TradingAccount.objects")
        pos_idx2 = daemon_close_src.find("Position.objects")
        self.assertGreater(acct_idx2, -1)
        self.assertGreater(pos_idx2, -1)
        self.assertLess(acct_idx2, pos_idx2)

    def test_admin_force_close_locks_account_before_position_source_order(self):
        from simulator import admin as admin_module

        src = inspect.getsource(admin_module)
        # Anchor to the force_close block specifically to avoid false
        # matches elsewhere in the (large) admin.py source.
        block_start = src.find('desk_action == "force_close"')
        self.assertGreater(block_start, -1)
        block = src[block_start:block_start + 2500]
        acct_idx = block.find("TradingAccount.objects.select_for_update()")
        pos_idx = block.find("Position.objects.select_for_update()")
        self.assertGreater(acct_idx, -1)
        self.assertGreater(pos_idx, -1)
        self.assertLess(acct_idx, pos_idx)

    def test_open_path_locks_account_before_position_source_order(self):
        open_src = inspect.getsource(TradingConsumer._db_open_position_atomic.__wrapped__)
        acct_idx = open_src.find("TradingAccount.objects")
        pos_idx = open_src.find("Position.objects.select_for_update()")
        self.assertGreater(acct_idx, -1)
        self.assertGreater(pos_idx, -1)
        self.assertLess(acct_idx, pos_idx)

    def test_dead_mirror_functions_have_no_call_sites(self):
        """The two functions excluded from the audit
        (_db_mirror_close_position, _db_mirror_open_or_update) must
        genuinely be unreachable — re-verified here so the exclusion
        can't silently go stale. Checks for an actual invocation pattern
        ("self.<name>(") rather than raw substring count, since the
        module-level LOCK ORDER comment legitimately names both functions
        in prose to document why they're excluded."""
        import simulator.consumers as consumers_module
        src = inspect.getsource(consumers_module)
        for name in ("_db_mirror_close_position", "_db_mirror_open_or_update"):
            self.assertNotIn(
                f"self.{name}(", src,
                f"{name} now has a call site — it must be brought into the "
                "audited Account→Position lock order, not left excluded.",
            )
            self.assertNotIn(f"await self.{name}(", src)


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Shared thread body: raises this connection's busy_timeout, waits on
    the barrier for maximum overlap, then calls fn() with retry-on-
    SQLITE_LOCKED (see module docstring — busy_timeout alone does not
    cover SQLite shared-cache table locks touched by ordinary reads
    inside a transaction holding a write lock elsewhere)."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = fn()
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    raise
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


def _open_in_thread(account_id, symbol, qty, price, barrier, results, index):
    def _do():
        return _db_open_sync(
            _consumer(account_id), symbol, "buy", qty, price, None, None,
            commission=0.0, new_balance=1_000_000.0,
        )
    _run_locked_retry(_do, barrier, results, index)


class FreshSnapshotZeroPositionsTests(TransactionTestCase):
    """TESTS OBLIGATORIOS #2 — caso cero posiciones: dos transacciones
    abren concurrentemente sobre una cuenta SIN posiciones previas; la
    segunda debe observar la Position creada por la primera DESPUÉS de
    obtener el lock de cuenta, no una foto tomada antes. Probado
    indirectamente pero de forma inequívoca: con max_open_positions=1,
    si T2 validara contra un snapshot pre-lock (positions=[]) en vez de
    re-consultar tras el lock, ambas se aceptarían (2 posiciones). Bajo
    el fix, exactamente 1 se acepta y la otra es rechazada específicamente
    por max_positions — la única forma de que eso ocurra es que la
    segunda transacción haya visto la Position de la primera."""

    def test_second_thread_observes_first_threads_commit_from_zero(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 1
        rule.save(update_fields=["max_open_positions"])
        self.assertEqual(Position.objects.filter(account=account).count(), 0)

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_open_in_thread,
                              args=(account.pk, "EUR/USD", 0.01, 1.17, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        accepted = [r for r in results if r and r["ok"]]
        rejected = [r for r in results if r and not r["ok"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "max_positions")
        self.assertEqual(Position.objects.filter(account=account).count(), 1)


class FreshSnapshotWithExistingPositionsTests(TransactionTestCase):
    """TESTS OBLIGATORIOS #3 — caso CON posiciones existentes: la segunda
    transacción debe ver cualquier inserción confirmada por la primera
    antes de su propia validación. 1 posición pre-existente,
    max_open_positions=2 → solo UNA de las dos conexiones concurrentes
    puede tomar el cupo restante; la otra debe ver 2 posiciones ya
    ocupando el límite y ser rechazada por max_positions."""

    def test_second_thread_sees_committed_insert_before_its_own_validation(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        _seed_fresh_price("GBP/USD", 1.2999, 1.3001)
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 2
        rule.save(update_fields=["max_open_positions"])
        make_position(account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.30"))

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_open_in_thread,
                              args=(account.pk, "EUR/USD", 0.01, 1.17, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        accepted = [r for r in results if r and r["ok"]]
        rejected = [r for r in results if r and not r["ok"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_code"], "max_positions")
        self.assertEqual(Position.objects.filter(account=account).count(), 2)


class RealMultiThreadedConcurrencyTests(TransactionTestCase):
    """TESTS OBLIGATORIOS #4 — the three PANEL-02 scenarios repeated end
    to end under the corrected Account→Position lock order, with genuine
    concurrent threads. See the module docstring for the SQLite-vs-
    PostgreSQL limitation."""

    def test_four_real_threads_with_35pct_pre_existing_never_exceed_50pct(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("1000.00"))
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("0.15"), avg_price=Decimal("1.17"))

        n = 4
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_open_in_thread,
                              args=(account.pk, "EUR/USD", 0.04, 1.17, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        accepted = sum(1 for r in results if r and r["ok"])
        total_margin = sum(
            abs(float(p.avg_price) * float(p.qty) * EURUSD_SPEC.contract_size) / 50
            for p in Position.objects.filter(account=account)
        )
        total_margin_pct = total_margin / 1000.0 * 100.0
        self.assertLessEqual(total_margin_pct, 50.0)
        self.assertEqual(accepted, 1)

    def test_six_real_threads_from_zero_only_accepts_available_capacity(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("1000.00"))

        n = 6
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_open_in_thread,
                              args=(account.pk, "EUR/USD", 0.04, 1.17, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        accepted = sum(1 for r in results if r and r["ok"])
        total_margin = sum(
            abs(float(p.avg_price) * float(p.qty) * EURUSD_SPEC.contract_size) / 50
            for p in Position.objects.filter(account=account)
        )
        self.assertLessEqual(total_margin / 1000.0 * 100.0, 50.0)
        self.assertEqual(accepted, 5)

    def test_max_open_positions_race_with_real_threads(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        _seed_fresh_price("GBP/USD", 1.2999, 1.3001)
        _seed_fresh_price("AUD/USD", 0.6799, 0.6801)
        _seed_fresh_price("USD/CAD", 1.3499, 1.3501)
        account = make_account(balance=Decimal("1_000_000.00"))
        from simulator.risk_engine import get_or_create_risk_rule
        rule = get_or_create_risk_rule(account)
        rule.max_open_positions = 2
        rule.save(update_fields=["max_open_positions"])
        make_position(account, symbol="GBP/USD", side="BUY",
                      qty=Decimal("0.01"), avg_price=Decimal("1.30"))

        symbols_and_prices = [("EUR/USD", 1.17), ("AUD/USD", 0.68), ("USD/CAD", 1.35)]
        n = len(symbols_and_prices)
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(
                target=_open_in_thread,
                args=(account.pk, sym, 0.01, px, barrier, results, i),
            )
            for i, (sym, px) in enumerate(symbols_and_prices)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        accepted = sum(1 for r in results if r and r["ok"])
        self.assertEqual(Position.objects.filter(account=account).count(), 2)
        self.assertEqual(accepted, 1)


class OpenVersusCloseConcurrencyTests(TransactionTestCase):
    """TESTS OBLIGATORIOS #5 — open concurrente con close sobre la MISMA
    cuenta: sin deadlock, usando el mismo orden global (ambos locan
    Account primero). Una posición existente se cierra en un hilo
    mientras otro abre una posición nueva en el mismo instante."""

    def test_concurrent_open_and_close_no_deadlock(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        _seed_fresh_price("GBP/USD", 1.2999, 1.3001)
        account = make_account(balance=Decimal("10000.00"))
        existing = make_position(account, symbol="GBP/USD", side="BUY",
                                  qty=Decimal("0.01"), avg_price=Decimal("1.30"))

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n

        def _open():
            return _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 0.01, 1.17, None, None,
                commission=0.0, new_balance=10000.0,
            )

        def _close():
            return _db_close_sync(
                _consumer(account.pk), _pos_mem(existing), 1.3050, "manual",
                5.0, 10005.0, 10005.0,
            )

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_open, barrier, results, 0)),
            threading.Thread(target=_run_locked_retry, args=(_close, barrier, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # No deadlock: both threads completed (results populated, no hang —
        # join(timeout=10) would leave a None here if a thread never
        # returned).
        self.assertIsNotNone(results[0], "open thread did not complete — possible deadlock")
        self.assertIsNotNone(results[1], "close thread did not complete — possible deadlock")
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1].get("already_closed"))
        # Final state: the GBP/USD position closed, the new EUR/USD one open.
        remaining = Position.objects.filter(account=account)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().symbol, "EUR/USD")


class CloseVersusDaemonConcurrencyTests(TransactionTestCase):
    """TESTS OBLIGATORIOS #6 — close concurrente con daemon sobre la
    MISMA posición: sin deadlock, sin doble cierre. Un hilo cierra vía el
    path del consumer WS, el otro vía el path síncrono del daemon
    (tasks._close_position_sync) — exactamente uno debe realizar el
    cierre real (crear Trade, borrar Position); el otro debe ver
    already_closed=True, sin Trade/LedgerEntry duplicados."""

    def test_concurrent_ws_close_and_daemon_close_no_double_close(self):
        _seed_fresh_price("EUR/USD", 1.1699, 1.1701)
        account = make_account(balance=Decimal("10000.00"))
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("1.17"))
        pos_mem = _pos_mem(pos)

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n

        def _ws_close():
            return _db_close_sync(
                _consumer(account.pk), pos_mem, 1.1750, "manual", 5.0, 10005.0, 10005.0,
            )

        def _daemon_close():
            return _close_position_sync(
                pos_mem, account.pk, 1.1750, "manual", 5.0, 10005.0, 10005.0,
            )

        threads = [
            threading.Thread(target=_run_locked_retry, args=(_ws_close, barrier, results, 0)),
            threading.Thread(target=_run_locked_retry, args=(_daemon_close, barrier, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertIsNotNone(results[0], "WS close thread did not complete — possible deadlock")
        self.assertIsNotNone(results[1], "daemon close thread did not complete — possible deadlock")

        already_closed_flags = [bool(r.get("already_closed")) for r in results]
        # Exactly one of the two performed the real close.
        self.assertEqual(already_closed_flags.count(False), 1)
        self.assertEqual(already_closed_flags.count(True), 1)

        from simulator.models import Trade, LedgerEntry
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(),
            1,
        )
        self.assertEqual(Position.objects.filter(account=account).count(), 0)


class PostgreSQLStagingValidationTests(TransactionTestCase):
    """MANDATORY pre-production step — NOT executable in this environment
    (no PostgreSQL server available: psql/pg_ctl/postgres all absent).

    Before this fix ships, re-run the concurrency scenarios in this file
    against a real staging PostgreSQL instance to confirm the same
    outcomes hold under genuine MVCC row-level locking (SQLite's coarser
    table/file-level locking, worked around above with retry-on-
    SQLITE_LOCKED, cannot certify this on its own — see the module
    docstring).

    How to run this file against PostgreSQL:
      1. Point Django at a staging PostgreSQL instance by setting DB_NAME
         (and DB_USER/DB_PASSWORD/DB_HOST/DB_PORT as needed) per
         trx_simulator/settings.py's existing DATABASES logic — no
         settings.py change needed, it already prefers PostgreSQL
         whenever DB_NAME is set.
      2. Run: DB_NAME=trx_staging python manage.py test
         simulator.tests.test_atomic_guard_lock_order
      3. Expected: identical pass/fail results to the SQLite run above,
         PLUS confirmation (e.g. via `SELECT * FROM pg_locks` during a
         paused debugger session, or pg_stat_activity) that concurrent
         opens for two DIFFERENT accounts never block each other — the
         one claim this SQLite suite cannot make.
      4. The retry-on-locked wrapper (_run_locked_retry) becomes
         unnecessary under PostgreSQL (real row locks block/queue instead
         of raising) but is harmless to leave in — a blocked transaction
         simply returns on its first attempt once unblocked.
    """

    @unittest.skip(
        "Documentation-only placeholder — no PostgreSQL server available in "
        "this environment. See class docstring for the mandatory staging "
        "PostgreSQL validation steps required before this fix ships."
    )
    def test_run_this_suite_against_postgresql_before_production(self):
        pass  # pragma: no cover — see class docstring.
