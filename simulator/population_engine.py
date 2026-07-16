"""
Population simulation engine.

Runs simulated traders in background threads, writing directly to the DB
(no WebSocket). Designed to stress-test the full broker ecosystem:
  - exposure_engine reads Position/Trade → sees sim traffic
  - risk_engine is called post-close → real DD/daily-loss logic
  - intelligence_engine classifies sim accounts → routing tested
  - All from a separate process (management command) — zero impact on Daphne.

Usage:
    python manage.py populate_broker --preset mini --speed 5
    python manage.py populate_broker --normal 5 --gambler 3 --scalper 4 --duration 300
    python manage.py populate_broker --status
    python manage.py populate_broker --reset
    python manage.py populate_broker --clean
"""

import logging
import random
import threading
import time
from decimal import Decimal

from django.db import close_old_connections, connection, transaction
from django.utils import timezone

log = logging.getLogger("simulator.population")

# ─────────────────────────────────────────────────────────────────────────────
# Profile configurations
# Each profile drives: tick frequency, open/close probabilities, lot sizes,
# win rate, allowed symbols, position count limits.
# ─────────────────────────────────────────────────────────────────────────────

PROFILES: dict[str, dict] = {
    "NORMAL": {
        "tick_s":     20,       # seconds between decisions (real time, before speed factor)
        "open_prob":  0.20,
        "close_prob": 0.18,
        "min_hold_s": 120,      # don't close before this many seconds
        "max_hold_s": 1800,     # force-close after this
        "base_lot":   Decimal("0.02"),
        "lot_jitter": Decimal("0.01"),
        "win_rate":   0.46,
        "symbols":    ["BTCUSD", "ETHUSD", "EUR/USD", "GBP/USD"],
        "max_pos":    2,
        "martingale": False,
    },
    "GAMBLER": {
        "tick_s":     7,
        "open_prob":  0.50,
        "close_prob": 0.30,
        "min_hold_s": 15,
        "max_hold_s": 300,
        "base_lot":   Decimal("0.06"),
        "lot_jitter": Decimal("0.02"),
        "win_rate":   0.30,
        "symbols":    ["BTCUSD", "BTCUSD", "BTCUSD", "ETHUSD"],  # heavy BTC bias
        "max_pos":    4,
        "martingale": True,
        "mart_factor": Decimal("1.8"),
        "mart_max":    Decimal("0.20"),
    },
    "SCALPER": {
        "tick_s":     3,
        "open_prob":  0.65,
        "close_prob": 0.75,
        "min_hold_s": 8,
        "max_hold_s": 90,
        "base_lot":   Decimal("0.01"),
        "lot_jitter": Decimal("0.00"),
        "win_rate":   0.41,
        "symbols":    ["BTCUSD", "ETHUSD", "EUR/USD", "GBP/USD", "EUR/USD"],
        "max_pos":    5,
        "martingale": False,
    },
    "MARTINGALE": {
        "tick_s":     40,
        "open_prob":  0.22,
        "close_prob": 0.08,
        "min_hold_s": 500,
        "max_hold_s": 5400,
        "base_lot":   Decimal("0.01"),
        "lot_jitter": Decimal("0.00"),
        "win_rate":   0.38,
        "symbols":    ["BTCUSD"],
        "max_pos":    1,
        "martingale": True,
        "mart_factor": Decimal("2.0"),
        "mart_max":    Decimal("0.24"),
    },
    "CONSISTENT": {
        "tick_s":     80,
        "open_prob":  0.12,
        "close_prob": 0.10,
        "min_hold_s": 600,
        "max_hold_s": 7200,
        "base_lot":   Decimal("0.02"),
        "lot_jitter": Decimal("0.00"),
        "win_rate":   0.57,
        "symbols":    ["BTCUSD", "EUR/USD", "ETHUSD"],
        "max_pos":    3,
        "martingale": False,
    },
    "ELITE": {
        "tick_s":     180,
        "open_prob":  0.08,
        "close_prob": 0.12,
        "min_hold_s": 900,
        "max_hold_s": 14400,
        "base_lot":   Decimal("0.03"),
        "lot_jitter": Decimal("0.01"),
        "win_rate":   0.67,
        "symbols":    ["BTCUSD", "ETHUSD"],
        "max_pos":    2,
        "martingale": False,
    },
}

# Presets for quick testing
PRESETS = {
    "mini":     {"NORMAL": 2, "GAMBLER": 1, "SCALPER": 2, "MARTINGALE": 1, "CONSISTENT": 1, "ELITE": 1},
    "standard": {"NORMAL": 5, "GAMBLER": 3, "SCALPER": 5, "MARTINGALE": 2, "CONSISTENT": 4, "ELITE": 2},
    "stress":   {"NORMAL": 8, "GAMBLER": 6, "SCALPER": 10, "MARTINGALE": 4, "CONSISTENT": 6, "ELITE": 4},
}

# Fallback prices when FeedManager is unavailable
_PRICE_FALLBACK = {
    "BTCUSD": 82000.0, "ETHUSD": 3400.0,
    "EUR/USD": 1.17, "GBP/USD": 1.30,
    "USD/JPY": 155.0, "AUD/USD": 0.68,
}


def _get_price(symbol: str) -> float:
    """Get current price from FeedManager or fall back to cached defaults."""
    try:
        from market_data.feeds import FeedManager
        fm = FeedManager.instance()
        p = fm.last_price(symbol)
        if p and p > 0:
            return float(p)
    except Exception:
        pass
    return _PRICE_FALLBACK.get(symbol, 1.17)


# ─────────────────────────────────────────────────────────────────────────────
# SimulatedTrader — one thread per account
# ─────────────────────────────────────────────────────────────────────────────

class SimulatedTrader(threading.Thread):
    """
    Runs a single simulated trading account in its own daemon thread.
    Writes directly to Position / Trade / LedgerEntry — no WebSocket.
    """

    def __init__(self, account_id: int, profile_name: str, speed: float = 1.0):
        super().__init__(
            daemon=True,
            name=f"sim-{profile_name[:3].lower()}-{account_id}",
        )
        self.account_id = account_id
        self.profile_name = profile_name
        self.cfg = PROFILES[profile_name]
        self.speed = max(0.5, min(speed, 50.0))  # clamp 0.5x – 50x

        self._stop = threading.Event()
        self._rng = random.Random(account_id * 7919 + hash(profile_name) % 9973)

        # Martingale state (reset to base_lot on each win)
        self._cur_lot: Decimal = self.cfg["base_lot"]
        self._consec_losses: int = 0

    # ── helpers ─────────────────────────────────────────────────────────────

    def _choose_symbol(self) -> str:
        return self._rng.choice(self.cfg["symbols"])

    def _choose_side(self) -> str:
        return self._rng.choice(["BUY", "SELL"])

    def _choose_lot(self) -> Decimal:
        jitter = self.cfg["lot_jitter"]
        raw = float(self._cur_lot) + self._rng.uniform(-float(jitter), float(jitter))
        return Decimal(str(round(max(0.01, raw), 2)))  # min 0.01, 2dp for Trade.lot_size

    def _simulate_pnl(
        self, entry: float, side: str, symbol: str, qty: float, account_currency: str = "USD",
    ) -> tuple[float, float]:
        """Return (close_price, realized_pnl) based on profile win_rate.

        Only the CLOSE PRICE is this simulation's own logic (a synthetic
        win/loss roll) — the PnL itself is delegated to
        simulator.pnl_engine.position_pnl_float() (MARGIN-02), never
        recomputed here. Before this fix, this function's own inline
        formula was missing BOTH contract_size AND currency conversion —
        wrong for every non-1:1-contract-size symbol (all of forex) and
        doubly wrong for USD/JPY specifically, despite already branching
        on `symbol.endswith("/JPY")` for its delta range. This writes real
        Trade/Position/TradingAccount.balance rows (via `manage.py
        populate_broker`), read by risk_engine/intelligence_engine/
        exposure_engine — not a display-only path."""
        wins = self._rng.random() < self.cfg["win_rate"]

        if symbol in ("BTCUSD", "ETHUSD"):
            delta = self._rng.uniform(40, 700)
        elif symbol.endswith("/JPY"):
            delta = self._rng.uniform(0.10, 2.0)
        else:
            delta = self._rng.uniform(0.0004, 0.004)

        if side == "BUY":
            close = entry + delta if wins else entry - delta
        else:
            close = entry - delta if wins else entry + delta

        close = max(close, entry * 0.75)  # sanity floor (prevent negative prices)

        from . import pnl_engine
        pnl = pnl_engine.position_pnl_float(side, entry, close, qty, symbol, account_currency=account_currency)

        dec = 2 if symbol in ("BTCUSD", "ETHUSD") else 5
        return round(close, dec), round(pnl, 2)

    def _should_close(self, pos) -> bool:
        """True if position is eligible for closing (past min_hold, within random probability)."""
        hold_real = (timezone.now() - pos.opened_at).total_seconds()
        min_real = self.cfg["min_hold_s"] / self.speed
        max_real = self.cfg["max_hold_s"] / self.speed
        if hold_real >= max_real:
            return True  # force-close
        if hold_real < min_real:
            return False
        return self._rng.random() < self.cfg["close_prob"]

    # ── actions ─────────────────────────────────────────────────────────────

    def _open_position(self, account) -> bool:
        """Open a new position. Returns True if executed."""
        from .models import Position, LedgerEntry, TradingAccount

        symbol = self._choose_symbol()
        side   = self._choose_side()
        lot    = self._choose_lot()
        price  = _get_price(symbol)
        lev    = 50
        margin_needed = float(lot) * price / lev
        commission    = round(float(lot) * price * 0.0002, 4)

        with transaction.atomic():
            acc = TradingAccount.objects.select_for_update().get(id=account.id)
            if acc.status != "Activo":
                return False
            # Simple margin guard (2× safety factor, no account suspension)
            if float(acc.balance) < margin_needed * 2:
                return False

            Position.objects.create(
                account=acc,
                symbol=symbol,
                side=side,
                qty=lot,
                avg_price=Decimal(str(price)),
                external_id=f"sim-{self.account_id}-{int(time.time()*1000)%999999}",
            )

            if commission > 0:
                new_bal = Decimal(str(float(acc.balance) - commission))
                TradingAccount.objects.filter(id=acc.id).update(balance=new_bal)
                LedgerEntry.objects.create(
                    account=acc,
                    event_type=LedgerEntry.EV_COMMISSION,
                    amount=Decimal(str(-commission)),
                    balance_after=new_bal,
                    meta={"symbol": symbol, "side": side,
                          "sim": True, "profile": self.profile_name},
                )

        log.debug("[sim:%s #%d] OPEN %s %s qty=%s @ %.2f",
                  self.profile_name, self.account_id, side, symbol, lot, price)
        return True

    def _close_position(self, account, pos) -> None:
        """Close a position, record Trade, update balance, run risk + intelligence."""
        from .models import Position, Trade, LedgerEntry, TradingAccount

        entry  = float(pos.avg_price)
        side   = pos.side
        qty    = float(pos.qty)
        symbol = pos.symbol

        close_px, pnl = self._simulate_pnl(
            entry, side, symbol, qty, account_currency=getattr(account, "currency", "USD") or "USD",
        )

        with transaction.atomic():
            acc = TradingAccount.objects.select_for_update().get(id=account.id)
            new_bal = float(acc.balance) + pnl
            new_peak = max(float(acc.peak_balance), new_bal)

            sim_trade = Trade.objects.create(
                account=acc,
                symbol=symbol,
                trade_type=side,
                lot_size=pos.qty,
                entry_price=pos.avg_price,
                exit_price=Decimal(str(close_px)),
                profit_loss=Decimal(str(pnl)),
                opened_at=pos.opened_at,
                closed_at=timezone.now(),
            )

            LedgerEntry.objects.create(
                account=acc,
                event_type=LedgerEntry.EV_REALIZED,
                amount=Decimal(str(pnl)),
                balance_after=Decimal(str(new_bal)),
                meta={"symbol": symbol, "side": side, "pnl": pnl,
                      "sim": True, "profile": self.profile_name},
            )

            # BOOK-02 — broker's B-Book counterparty result for this same
            # Trade, same transaction. Simulated accounts write real Trade/
            # LedgerEntry rows (counted by intelligence_engine/exposure_engine
            # like any other account), so they get a real counterparty entry too.
            from .broker_ledger import create_broker_counterparty_entry
            create_broker_counterparty_entry(sim_trade, acc, pnl, "population_sim")

            Position.objects.filter(id=pos.id).delete()

            TradingAccount.objects.filter(id=acc.id).update(
                balance=Decimal(str(new_bal)),
                peak_balance=Decimal(str(new_peak)),
            )

            # Run risk engine post-close (real DD / daily-loss enforcement)
            acc.refresh_from_db()
            from .risk_engine import check_and_enforce_risk
            check_and_enforce_risk(acc)

        # Update martingale lot-sizing state
        if pnl < 0:
            self._consec_losses += 1
            if self.cfg["martingale"]:
                self._cur_lot = min(
                    self._cur_lot * self.cfg["mart_factor"],
                    self.cfg["mart_max"],
                )
        else:
            self._consec_losses = 0
            self._cur_lot = self.cfg["base_lot"]

        # Intelligence engine — classify trader after every 3rd close (approx)
        if self._rng.random() < 0.33:
            try:
                from .models import TradingAccount as TA
                fresh = TA.objects.get(id=self.account_id)
                from .intelligence_engine import update_intelligence
                update_intelligence(fresh)
            except Exception as exc:
                log.debug("[sim] intelligence update error: %s", exc)

        log.debug("[sim:%s #%d] CLOSE %s %s pnl=%.2f new_bal=%.2f",
                  self.profile_name, self.account_id, side, symbol, pnl, new_bal)

    # ── tick ────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        from .models import TradingAccount, Position

        acc = TradingAccount.objects.get(id=self.account_id)
        if acc.status != "Activo":
            return  # suspended / violated — do nothing

        positions = list(Position.objects.filter(account=acc).order_by("opened_at"))
        n_open = len(positions)
        max_pos = self.cfg["max_pos"]

        # Force-close any position past its max hold time (by real-time)
        for pos in positions:
            hold = (timezone.now() - pos.opened_at).total_seconds()
            if hold >= self.cfg["max_hold_s"] / self.speed:
                self._close_position(acc, pos)
                return  # one action per tick

        if n_open >= max_pos:
            # Fully loaded — try to close an eligible position
            eligible = [p for p in positions if self._should_close(p)]
            if eligible:
                self._close_position(acc, self._rng.choice(eligible))

        elif n_open == 0:
            # No positions — higher open probability
            if self._rng.random() < self.cfg["open_prob"] * 1.5:
                self._open_position(acc)

        else:
            # Mixed — roll for open or close
            r = self._rng.random()
            if r < self.cfg["close_prob"]:
                eligible = [p for p in positions if self._should_close(p)]
                if eligible:
                    self._close_position(acc, self._rng.choice(eligible))
            elif r < self.cfg["close_prob"] + self.cfg["open_prob"]:
                if n_open < max_pos:
                    self._open_position(acc)

    # ── main loop ───────────────────────────────────────────────────────────

    def run(self) -> None:
        interval = self.cfg["tick_s"] / self.speed
        log.info("[sim] started %s #%d  interval=%.2fs  speed=%.1fx",
                 self.profile_name, self.account_id, interval, self.speed)

        while not self._stop.wait(interval):
            try:
                # Recycle any DB connection that was closed by the server
                # (e.g. Postgres idle-in-transaction timeout) before every tick.
                close_old_connections()
                self._tick()
            except Exception as exc:
                log.error("[sim:%s #%d] tick error: %s",
                          self.profile_name, self.account_id, exc, exc_info=False)

        # Release the thread-local connection on clean exit so Postgres does not
        # count it as an idle connection indefinitely.
        try:
            connection.close()
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# PopulationRunner — manages the trader thread pool
# ─────────────────────────────────────────────────────────────────────────────

_SIM_USER = "__sim__"  # internal Django user for all simulated accounts


class PopulationRunner:
    """Thread-pool manager for simulated traders."""

    _lock = threading.Lock()
    _threads: list[SimulatedTrader] = []
    _running: bool = False

    # ── account management ──────────────────────────────────────────────────

    @classmethod
    def create_accounts(
        cls, profile_counts: dict[str, int], tier: str = "10K"
    ) -> list[int]:
        """Create DB accounts for each profile. Returns list of new account IDs."""
        from django.contrib.auth import get_user_model
        from .models import TradingAccount

        User = get_user_model()
        sim_user, _ = User.objects.get_or_create(
            username=_SIM_USER,
            defaults={"is_active": False, "email": "sim@internal.broker"},
        )

        tier_balances = {"10K": Decimal("10000"), "50K": Decimal("50000"), "100K": Decimal("100000")}
        init = tier_balances.get(tier, Decimal("10000"))
        created: list[int] = []

        for profile, count in profile_counts.items():
            if profile not in PROFILES or count <= 0:
                continue
            for _ in range(count):
                acc = TradingAccount.objects.create(
                    user=sim_user,
                    tier=tier,
                    phase=f"Sim:{profile}",
                    balance=init,
                    equity=init,
                    peak_balance=init,
                    status="Activo",
                    leverage=50,
                    netting_mode=False,  # hedging: separate position per trade
                )
                created.append(acc.id)
                log.info("[sim] created account #%d  profile=%s  tier=%s",
                         acc.id, profile, tier)

        return created

    # ── lifecycle ────────────────────────────────────────────────────────────

    @classmethod
    def start(
        cls,
        profile_counts: dict[str, int],
        speed: float = 1.0,
        tier: str = "10K",
    ) -> list[SimulatedTrader]:
        with cls._lock:
            if cls._running:
                log.warning("[sim] already running — call stop() first")
                return cls._threads

            ids = cls.create_accounts(profile_counts, tier)
            if not ids:
                log.error("[sim] no accounts created — nothing to run")
                return []

            from .models import TradingAccount
            threads: list[SimulatedTrader] = []
            for acc in TradingAccount.objects.filter(id__in=ids):
                profile = acc.phase.replace("Sim:", "")
                if profile not in PROFILES:
                    continue
                t = SimulatedTrader(acc.id, profile, speed=speed)
                t.start()
                threads.append(t)

            cls._threads = threads
            cls._running = True
            log.info("[sim] started %d trader threads  speed=%.1fx", len(threads), speed)
            return threads

    @classmethod
    def stop(cls) -> None:
        with cls._lock:
            if not cls._threads:
                return
            for t in cls._threads:
                t.stop()
            for t in cls._threads:
                t.join(timeout=8)
            cls._threads = []
            cls._running = False
            log.info("[sim] all threads stopped")

    @classmethod
    def reset(cls) -> int:
        """
        Stop simulation, close all open positions, restore balances.
        Accounts are kept for re-use.
        """
        cls.stop()
        from django.contrib.auth import get_user_model
        from .models import TradingAccount, Position

        User = get_user_model()
        try:
            sim_user = User.objects.get(username=_SIM_USER)
        except User.DoesNotExist:
            return 0

        accs = TradingAccount.objects.filter(user=sim_user, phase__startswith="Sim:")
        n = 0
        for acc in accs:
            Position.objects.filter(account=acc).delete()
            init = acc.initial_balance or acc.balance or Decimal("10000")
            TradingAccount.objects.filter(id=acc.id).update(
                balance=init, equity=init, peak_balance=init, status="Activo"
            )
            n += 1
        log.info("[sim] reset %d accounts", n)
        return n

    @classmethod
    def delete_all(cls) -> int:
        """Stop simulation and delete all simulated accounts (cascades to positions/trades)."""
        cls.stop()
        from django.contrib.auth import get_user_model
        from .models import TradingAccount

        User = get_user_model()
        try:
            sim_user = User.objects.get(username=_SIM_USER)
        except User.DoesNotExist:
            return 0

        accs = TradingAccount.objects.filter(user=sim_user, phase__startswith="Sim:")
        n = accs.count()
        accs.delete()
        log.info("[sim] deleted %d accounts", n)
        return n

    @classmethod
    def status(cls) -> dict:
        """Return a snapshot of all simulated accounts grouped by profile."""
        from django.contrib.auth import get_user_model
        from .models import TradingAccount, Position, Trade

        User = get_user_model()
        try:
            sim_user = User.objects.get(username=_SIM_USER)
        except User.DoesNotExist:
            return {"running": False, "thread_count": 0, "total_accounts": 0, "profiles": {}}

        accs = list(TradingAccount.objects.filter(user=sim_user, phase__startswith="Sim:"))
        acc_ids = [a.id for a in accs]

        # Bulk-count positions per account
        from django.db.models import Count, Sum
        pos_counts = dict(
            Position.objects.filter(account_id__in=acc_ids)
            .values_list("account_id")
            .annotate(n=Count("id"))
            .values_list("account_id", "n")
        )

        profiles: dict = {}
        for acc in accs:
            profile = acc.phase.replace("Sim:", "")
            p = profiles.setdefault(profile, {
                "count": 0, "active": 0, "suspended": 0,
                "open_positions": 0, "balance_total": 0.0,
            })
            p["count"] += 1
            p["open_positions"] += pos_counts.get(acc.id, 0)
            p["balance_total"] += float(acc.balance)
            if acc.status == "Activo":
                p["active"] += 1
            else:
                p["suspended"] += 1

        return {
            "running": cls._running,
            "thread_count": len(cls._threads),
            "total_accounts": len(accs),
            "profiles": profiles,
        }
