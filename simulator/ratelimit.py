"""
simulator/ratelimit.py
Redis-based HTTP rate limiting for sensitive endpoints.

Design:
- Fixed window per (ip, endpoint_key) — simple and auditable
- Fail-open: if Redis is unavailable, the request is ALLOWED
- Never blocks legitimate traffic due to infrastructure failure
- Logs a security event on every rate limit hit
- Decorator-based: apply per-view, not globally

Usage:
    from .ratelimit import rate_limit

    @rate_limit("login", limit=8, window=300)
    def login_view(request): ...

    @rate_limit("withdraw", limit=5, window=60)
    def withdraw_view(request): ...
"""
import logging
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render

_log = logging.getLogger("simulator.security")
_RL_PREFIX = "trx:rl:"


def _get_rl_redis():
    """Return a cached Redis client from the observability singleton."""
    from django.conf import settings as _s
    from .observability import _get_redis
    url = getattr(_s, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
    return _get_redis(url)


def _load_test_mode() -> bool:
    """
    LOAD_TEST_MODE=True bypasses all rate limits during load testing.
    NEVER enable in production — only valid in staging .env files.
    Fail-safe: any error reading the setting returns False.
    """
    try:
        from django.conf import settings as _s
        return bool(getattr(_s, "LOAD_TEST_MODE", False))
    except Exception:
        return False


def rate_check(key: str, limit: int, window: int) -> tuple[bool, int]:
    """
    Increment the counter for `key` and check against `limit` within `window` seconds.
    Returns (allowed: bool, current_count: int).
    On Redis failure: returns (True, 0) — fail-open.
    On LOAD_TEST_MODE=True: returns (True, 0) — bypass for load testing.
    """
    if _load_test_mode():
        return True, 0
    try:
        r = _get_rl_redis()
        full_key = f"{_RL_PREFIX}{key}"
        pipe = r.pipeline()
        pipe.incr(full_key)
        results = pipe.execute()
        count = results[0]
        if count == 1:
            r.expire(full_key, window)
        return count <= limit, count
    except Exception as exc:
        _log.warning("[ratelimit] Redis unavailable for key=%s — fail-open: %r", key, exc)
        return True, 0


def _ip_from_request(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def rate_limit(endpoint_key: str, limit: int, window: int, by: str = "ip"):
    """
    Decorator factory for per-view rate limiting.

    Args:
        endpoint_key: short identifier for the endpoint, e.g. "login", "withdraw"
        limit:        max requests allowed per window
        window:       window duration in seconds
        by:           "ip" (default) — key is (endpoint_key, ip)
                      "user" — key is (endpoint_key, user_id); falls back to ip if anonymous

    On limit exceeded:
    - Returns 429 JSON for API/callback views (detected by endpoint_key prefix)
    - Returns 429 HTML for regular views (renders the login template with error)
    - Emits security_log("ratelimit.hit", ...) always
    """
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapper(request, *args, **kwargs):
            ip = _ip_from_request(request)

            if by == "user" and request.user.is_authenticated:
                dimension = f"u{request.user.pk}"
            else:
                dimension = ip

            rl_key = f"{endpoint_key}:{dimension}"
            allowed, count = rate_check(rl_key, limit, window)

            if not allowed:
                from .observability import security_log
                security_log(
                    "ratelimit.hit",
                    ip=ip,
                    endpoint=f"/{endpoint_key}/",
                    attempt_count=count,
                    username=getattr(request.user, "username", "anon") if request.user.is_authenticated else "anon",
                )
                _log.warning(
                    "[ratelimit] BLOCKED endpoint=%s ip=%s count=%d limit=%d window=%ds",
                    endpoint_key, ip, count, limit, window,
                )
                # JSON response for webhook/API endpoints
                if endpoint_key in ("deposit_callback", "withdraw_callback"):
                    return JsonResponse(
                        {"error": "rate_limited", "retry_after": window},
                        status=429,
                    )
                # HTML response for browser-facing views
                return render(
                    request,
                    "simulator/login.html",
                    {"error": f"Demasiados intentos. Espera {window // 60} minuto(s)."},
                    status=429,
                )

            return view_fn(request, *args, **kwargs)
        return wrapper
    return decorator
