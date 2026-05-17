"""
load_tests/ws_load.py
WebSocket load test for trx_sim — asyncio + websockets.

Simulates real WS client behaviour:
  1. Authenticate via HTTP (Django session cookie)
  2. Open WS connection with session cookie
  3. Send messages: ping, change_symbol, order:new (burst)
  4. Receive and count messages (ticks, candles, account updates)
  5. Measure connect latency, first-tick latency, message throughput
  6. Reconnect storm scenario: simultaneous disconnect + reconnect

Scenarios:
  --scenario connect     : N connections, each receives M ticks, then disconnects
  --scenario burst       : N connections each send K orders rapidly
  --scenario reconnect   : N connections disconnect/reconnect repeatedly
  --scenario sustained   : N connections stay open for T seconds

Prerequisites:
  - Daphne running (real ASGI, not runserver)
  - LOAD_TEST_MODE=True in .env (bypasses WS rate limiting)
  - Test users created (see locustfile.py for creation commands)

Usage:
  python load_tests/ws_load.py --host http://127.0.0.1:8001 \\
    --users 20 --scenario connect --ticks 30

  python load_tests/ws_load.py --host http://127.0.0.1:8001 \\
    --users 50 --scenario reconnect --reconnects 5

  python load_tests/ws_load.py --host http://127.0.0.1:8001 \\
    --users 30 --scenario sustained --duration 120
"""
import argparse
import asyncio
import json
import logging
import random
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ws_load")

_PASSWORD = "LoadTest123!"
_SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "BTCUSD", "ETHUSD"]


# ── Results collector ─────────────────────────────────────────

@dataclass
class WorkerResult:
    user_id: int
    connected: bool = False
    connect_latency_ms: float = 0.0
    first_tick_latency_ms: float = 0.0
    messages_received: int = 0
    orders_sent: int = 0
    errors: list = field(default_factory=list)
    reconnects: int = 0
    duration_s: float = 0.0


# ── HTTP auth: get session cookie from Django ─────────────────

async def _get_session_cookie(session: aiohttp.ClientSession, base_url: str, username: str) -> Optional[str]:
    """Login via HTTP and return the session cookie string."""
    login_url = f"{base_url}/login/"

    # Step 1: GET to seed CSRF cookie
    async with session.get(login_url, allow_redirects=True) as r:
        csrf = session.cookie_jar.filter_cookies(login_url).get("csrftoken")
        csrf_value = csrf.value if csrf else ""

    # Step 2: POST credentials
    async with session.post(
        login_url,
        data={
            "username": username,
            "password": _PASSWORD,
            "csrfmiddlewaretoken": csrf_value,
        },
        headers={"Referer": login_url},
        allow_redirects=True,
    ) as r:
        if r.status not in (200, 302):
            log.warning("[%s] Login HTTP %d", username, r.status)
            return None

    # Extract session cookie
    cookies = session.cookie_jar.filter_cookies(base_url)
    sess = cookies.get("sessionid")
    return f"sessionid={sess.value}" if sess else None


def _http_to_ws(base_url: str) -> str:
    """Convert http:// to ws://, https:// to wss://"""
    return base_url.replace("http://", "ws://").replace("https://", "wss://")


# ── Single WS worker ─────────────────────────────────────────

async def _ws_connect(base_url: str, ws_url: str, cookie: str) -> websockets.WebSocketClientProtocol:
    """Open a WebSocket connection with the session cookie."""
    symbol = random.choice(_SYMBOLS)
    full_url = f"{ws_url}/ws/trading/?symbol={urllib.parse.quote(symbol)}"
    ws = await websockets.connect(
        full_url,
        additional_headers={"Cookie": cookie},
        ping_interval=None,   # we manage our own heartbeat
        open_timeout=10,
        close_timeout=5,
        max_size=2**20,
    )
    return ws


async def _drain_until_ack(ws, timeout=10.0) -> bool:
    """Read messages until we receive the 'connected' ack."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            if msg.get("type") == "ack" and msg.get("action") == "connected":
                return True
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            break
    return False


# ── Scenario implementations ──────────────────────────────────

async def scenario_connect(args, user_n: int) -> WorkerResult:
    """Connect, wait for N ticks, disconnect."""
    result = WorkerResult(user_id=user_n)
    username = f"loadtest_{(user_n % 20) + 1}"
    ws_base = _http_to_ws(args.host)

    async with aiohttp.ClientSession() as http_sess:
        cookie = await _get_session_cookie(http_sess, args.host, username)
        if not cookie:
            result.errors.append("auth_failed")
            return result

        t0 = time.monotonic()
        try:
            ws = await _ws_connect(args.host, ws_base, cookie)
            result.connected = True
            result.connect_latency_ms = (time.monotonic() - t0) * 1000

            acked = await _drain_until_ack(ws)
            if not acked:
                result.errors.append("no_ack")

            # Wait for N price ticks
            first_tick = None
            for _ in range(args.ticks):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    msg = json.loads(raw)
                    result.messages_received += 1
                    if first_tick is None and msg.get("type") == "tick":
                        first_tick = time.monotonic()
                        result.first_tick_latency_ms = (first_tick - t0) * 1000
                except asyncio.TimeoutError:
                    result.errors.append("tick_timeout")
                    break

            await ws.close()

        except Exception as exc:
            result.errors.append(str(exc)[:80])

    result.duration_s = time.monotonic() - t0
    return result


async def scenario_burst(args, user_n: int) -> WorkerResult:
    """Connect and send a burst of orders, measuring rate-limit behavior."""
    result = WorkerResult(user_id=user_n)
    username = f"loadtest_{(user_n % 20) + 1}"
    ws_base = _http_to_ws(args.host)
    symbol = "EUR/USD"

    async with aiohttp.ClientSession() as http_sess:
        cookie = await _get_session_cookie(http_sess, args.host, username)
        if not cookie:
            result.errors.append("auth_failed")
            return result

        t0 = time.monotonic()
        try:
            ws = await _ws_connect(args.host, ws_base, cookie)
            result.connected = True
            await _drain_until_ack(ws)

            # Send burst of orders
            for i in range(args.orders):
                msg = json.dumps({
                    "action": "order:new",
                    "symbol": symbol,
                    "side": random.choice(["buy", "sell"]),
                    "qty": round(random.uniform(0.01, 0.1), 2),
                })
                await ws.send(msg)
                result.orders_sent += 1
                await asyncio.sleep(0.1)  # 100ms between orders

            # Drain responses
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    result.messages_received += 1
                except asyncio.TimeoutError:
                    break

            await ws.close()

        except Exception as exc:
            result.errors.append(str(exc)[:80])

    result.duration_s = time.monotonic() - t0
    return result


async def scenario_reconnect(args, user_n: int) -> WorkerResult:
    """Simulate reconnect storm: connect → disconnect → reconnect × N."""
    result = WorkerResult(user_id=user_n)
    username = f"loadtest_{(user_n % 20) + 1}"
    ws_base = _http_to_ws(args.host)

    async with aiohttp.ClientSession() as http_sess:
        cookie = await _get_session_cookie(http_sess, args.host, username)
        if not cookie:
            result.errors.append("auth_failed")
            return result

        t0 = time.monotonic()
        for i in range(args.reconnects):
            try:
                ws = await _ws_connect(args.host, ws_base, cookie)
                result.connected = True
                await _drain_until_ack(ws)

                # Brief connection — receive a few messages
                for _ in range(3):
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                        result.messages_received += 1
                    except asyncio.TimeoutError:
                        break

                await ws.close()
                result.reconnects += 1
                await asyncio.sleep(0.5)  # brief pause between reconnects

            except Exception as exc:
                result.errors.append(f"reconnect_{i}: {str(exc)[:60]}")
                await asyncio.sleep(1.0)

    result.duration_s = time.monotonic() - t0
    return result


async def scenario_sustained(args, user_n: int) -> WorkerResult:
    """Keep connection open for --duration seconds, measuring stability."""
    result = WorkerResult(user_id=user_n)
    username = f"loadtest_{(user_n % 20) + 1}"
    ws_base = _http_to_ws(args.host)

    async with aiohttp.ClientSession() as http_sess:
        cookie = await _get_session_cookie(http_sess, args.host, username)
        if not cookie:
            result.errors.append("auth_failed")
            return result

        t0 = time.monotonic()
        deadline = t0 + args.duration

        try:
            ws = await _ws_connect(args.host, ws_base, cookie)
            result.connected = True
            result.connect_latency_ms = (time.monotonic() - t0) * 1000
            await _drain_until_ack(ws)

            first_tick = None
            symbol_idx = 0

            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    msg = json.loads(raw)
                    result.messages_received += 1

                    if first_tick is None and msg.get("type") == "tick":
                        first_tick = time.monotonic()
                        result.first_tick_latency_ms = (first_tick - t0) * 1000

                    # Periodically change symbol to test feed manager
                    if result.messages_received % 60 == 0:
                        symbol_idx = (symbol_idx + 1) % len(_SYMBOLS)
                        await ws.send(json.dumps({
                            "action": "change_symbol",
                            "symbol": _SYMBOLS[symbol_idx],
                        }))

                    # Periodic ping
                    if result.messages_received % 30 == 0:
                        await ws.send(json.dumps({"action": "ping"}))

                except asyncio.TimeoutError:
                    # No message in 3s — send ping to keep alive
                    try:
                        await ws.send(json.dumps({"action": "ping"}))
                    except Exception:
                        result.errors.append("ping_failed")
                        break
                except websockets.exceptions.ConnectionClosed as exc:
                    result.errors.append(f"connection_closed: {exc.code}")
                    break

            await ws.close()

        except Exception as exc:
            result.errors.append(str(exc)[:80])

    result.duration_s = time.monotonic() - t0
    return result


# ── Runner + statistics ───────────────────────────────────────

SCENARIOS = {
    "connect":   scenario_connect,
    "burst":     scenario_burst,
    "reconnect": scenario_reconnect,
    "sustained": scenario_sustained,
}


def _print_report(results: list[WorkerResult], scenario: str, elapsed: float):
    total    = len(results)
    connected = sum(1 for r in results if r.connected)
    errors   = sum(len(r.errors) for r in results)
    msgs     = sum(r.messages_received for r in results)
    orders   = sum(r.orders_sent for r in results)

    connect_lats = [r.connect_latency_ms for r in results if r.connect_latency_ms > 0]
    tick_lats    = [r.first_tick_latency_ms for r in results if r.first_tick_latency_ms > 0]

    print(f"\n{'='*60}")
    print(f"  WebSocket Load Test Report — {scenario}")
    print(f"{'='*60}")
    print(f"  Total users:        {total}")
    print(f"  Connected:          {connected}  ({connected/total*100:.0f}%)")
    print(f"  Total errors:       {errors}")
    print(f"  Messages received:  {msgs}")
    if orders:
        print(f"  Orders sent:        {orders}")
    print(f"  Elapsed:            {elapsed:.1f}s")

    if connect_lats:
        print(f"\n  Connect latency (ms):")
        print(f"    min={min(connect_lats):.0f}  "
              f"p50={statistics.median(connect_lats):.0f}  "
              f"p95={sorted(connect_lats)[int(len(connect_lats)*0.95)]:.0f}  "
              f"max={max(connect_lats):.0f}")

    if tick_lats:
        print(f"\n  First tick latency (ms):")
        print(f"    min={min(tick_lats):.0f}  "
              f"p50={statistics.median(tick_lats):.0f}  "
              f"p95={sorted(tick_lats)[int(len(tick_lats)*0.95)]:.0f}  "
              f"max={max(tick_lats):.0f}")

    # Per-worker errors
    error_workers = [(r.user_id, r.errors) for r in results if r.errors]
    if error_workers:
        print(f"\n  Errors by worker:")
        for uid, errs in error_workers[:10]:
            print(f"    worker={uid}: {errs}")
        if len(error_workers) > 10:
            print(f"    ... and {len(error_workers)-10} more workers with errors")

    print(f"{'='*60}\n")


async def _run(args):
    scenario_fn = SCENARIOS[args.scenario]

    log.info(
        "Starting %s scenario: %d users → %s",
        args.scenario, args.users, args.host
    )

    t0 = time.monotonic()

    # Stagger connections over 2 seconds to avoid thundering herd
    tasks = []
    for i in range(args.users):
        await asyncio.sleep(2.0 / max(args.users, 1))
        tasks.append(asyncio.create_task(scenario_fn(args, i + 1)))

    results = await asyncio.gather(*tasks, return_exceptions=False)
    elapsed = time.monotonic() - t0

    _print_report(results, args.scenario, elapsed)


def main():
    parser = argparse.ArgumentParser(description="trx_sim WebSocket load test")
    parser.add_argument("--host",      default="http://127.0.0.1:8001", help="Base HTTP URL of the server")
    parser.add_argument("--users",     type=int, default=10,   help="Number of concurrent WS users")
    parser.add_argument("--scenario",  choices=list(SCENARIOS), default="connect", help="Test scenario")
    # Scenario-specific params
    parser.add_argument("--ticks",     type=int, default=30,   help="[connect] ticks to wait per user")
    parser.add_argument("--orders",    type=int, default=12,   help="[burst] orders per user")
    parser.add_argument("--reconnects",type=int, default=5,    help="[reconnect] reconnects per user")
    parser.add_argument("--duration",  type=int, default=60,   help="[sustained] seconds per user")

    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
