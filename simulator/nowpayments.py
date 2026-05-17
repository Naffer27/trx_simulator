# simulator/nowpayments.py
"""
NowPayments API service layer.

All HTTP calls, credential loading, and IPN signature verification live here.
Views must not import `requests` or read os.environ for NP keys directly.

Currency codes live in currencies.py — the single source of truth.
"""

import hashlib
import hmac as _hmac
import json
import logging
import os
from decimal import Decimal
from urllib.parse import urlparse

import requests

from .currencies import CURRENCY_MAP, CRYPTO_CHOICES, to_np_code, WITHDRAWAL_CURRENCY_MAP  # re-export for convenience

logger = logging.getLogger(__name__)

_BASE = "https://api.nowpayments.io/v1"


# ─────────────────────────────────────────
# Credentials (never hard-coded)
# ─────────────────────────────────────────

def _api_key() -> str:
    key = os.getenv("NOWPAYMENTS_API_KEY", "")
    if not key:
        raise ValueError("NOWPAYMENTS_API_KEY is not configured")
    return key


def _ipn_secret() -> str:
    secret = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
    if not secret:
        raise ValueError("NOWPAYMENTS_IPN_SECRET is not configured")
    return secret


def _headers() -> dict:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")

def _resolve_callback_url(generated_url: str) -> str | None:
    """
    Return the callback URL to send to NowPayments.

    Priority:
      1. NOWPAYMENTS_CALLBACK_URL env var (set this to an ngrok/tunnel URL in dev)
      2. The Django-generated URL — but only if it's a public host.
         Localhost/127.0.0.1 URLs are silently dropped (NP rejects them with 500).

    Returns None when no usable URL is available, which causes the ipn_callback_url
    field to be omitted from the payload entirely.
    """
    override = os.getenv("NOWPAYMENTS_CALLBACK_URL", "").strip()
    if override:
        logger.info("[NP] using NOWPAYMENTS_CALLBACK_URL override: %s", override)
        return override

    host = urlparse(generated_url).hostname or ""
    if host in _PRIVATE_HOSTS or host.endswith(".local"):
        logger.warning(
            "[NP] callback URL %s is a private/localhost address — "
            "omitting ipn_callback_url from payload. "
            "Set NOWPAYMENTS_CALLBACK_URL to an ngrok tunnel for dev testing.",
            generated_url,
        )
        return None

    return generated_url


# ─────────────────────────────────────────
# Payments
# ─────────────────────────────────────────

def create_payment(
    amount_usd: Decimal | float,
    pay_currency: str,
    deposit_id: int,
    callback_url: str,
) -> dict:
    """
    POST /v1/payment — create a new crypto payment.

    pay_currency is the DB key (e.g. 'btc', 'usdttrc20').
    It is translated to the exact NP code via to_np_code() before sending.

    Returns the full NowPayments response dict which includes:
      payment_id, payment_status, pay_address, pay_amount, pay_currency,
      expiration_estimate_date, invoice_url
    """
    np_code      = to_np_code(pay_currency)
    key          = _api_key()
    resolved_cb  = _resolve_callback_url(callback_url)

    payload: dict = {
        "price_amount":      float(amount_usd),
        "price_currency":    "usd",
        "pay_currency":      np_code,
        "order_id":          str(deposit_id),
        "order_description": f"Money Brokers Deposit #{deposit_id}",
    }
    if resolved_cb:
        payload["ipn_callback_url"] = resolved_cb

    logger.info(
        "[NP] → POST /payment deposit_id=%d np_code=%s callback=%s payload=%s",
        deposit_id, np_code,
        resolved_cb or "(omitted—localhost)",
        json.dumps(payload),
    )
    resp = requests.post(
        f"{_BASE}/payment",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    logger.info("[NP] ← HTTP %d body=%s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "[NP] invoice OK deposit_id=%d payment_id=%s status=%s "
        "pay_address=%s… pay_amount=%s %s",
        deposit_id,
        data.get("payment_id", "?"),
        data.get("payment_status", "?"),
        str(data.get("pay_address", ""))[:12],
        data.get("pay_amount", "?"),
        data.get("pay_currency", "").upper(),
    )
    return data


def get_payment_status(payment_id: str) -> dict:
    """GET /v1/payment/<id> — fetch current status of a payment."""
    resp = requests.get(
        f"{_BASE}/payment/{payment_id}",
        headers={"x-api-key": _api_key()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.debug(
        "[NP] status poll payment_id=%s → %s",
        payment_id, data.get("payment_status", "?"),
    )
    return data


_NP_CURRENCIES_CACHE_KEY = "np_supported_currencies"
_NP_CURRENCIES_CACHE_TTL = 1800  # 30 minutes


def get_available_currencies(force_refresh: bool = False) -> list[str]:
    """
    GET /v1/currencies — return list of NP-supported pay_currency codes.
    Result is cached in Django's cache backend for 30 min.
    """
    from django.core.cache import cache
    if not force_refresh:
        cached = cache.get(_NP_CURRENCIES_CACHE_KEY)
        if cached is not None:
            return cached
    resp = requests.get(
        f"{_BASE}/currencies",
        headers={"x-api-key": _api_key()},
        timeout=10,
    )
    resp.raise_for_status()
    currencies = resp.json().get("currencies", [])
    cache.set(_NP_CURRENCIES_CACHE_KEY, currencies, timeout=_NP_CURRENCIES_CACHE_TTL)
    logger.info("[NP] fetched %d supported currencies (cached 30 min)", len(currencies))
    return currencies


def check_our_currencies() -> dict[str, str]:
    """
    Validate every key in CURRENCY_MAP against the live NP currencies list.
    Returns {db_key: "OK" | "NOT SUPPORTED"}.
    """
    try:
        live = set(get_available_currencies())
    except Exception as exc:
        return {k: f"ERROR fetching NP list: {exc}" for k in CURRENCY_MAP}
    return {
        db_key: ("OK" if np_code in live else f"NOT SUPPORTED (np_code={np_code})")
        for db_key, (np_code, _) in CURRENCY_MAP.items()
    }


def api_status() -> dict:
    """
    Diagnostic: check API key, connectivity, and which of our currency codes
    are actually supported by NowPayments.

    Returns a dict safe to serialize as JSON.
    """
    result: dict = {}

    # ── API key presence ──────────────────────────────────────────────────
    try:
        key = _api_key()
        result["api_key"] = f"{key[:6]}…  (len={len(key)})"
    except ValueError as exc:
        result["api_key"] = f"MISSING — {exc}"
        return result

    # ── IPN secret presence ───────────────────────────────────────────────
    try:
        sec = _ipn_secret()
        result["ipn_secret"] = f"{sec[:4]}…  (len={len(sec)})"
    except ValueError as exc:
        result["ipn_secret"] = f"MISSING — {exc}"

    # ── /v1/status — basic connectivity ──────────────────────────────────
    try:
        r = requests.get(f"{_BASE}/status", timeout=8)
        result["api_reachable"] = r.status_code
        result["api_status_body"] = r.json()
    except Exception as exc:
        result["api_reachable"] = f"ERROR: {exc}"

    # ── /v1/currencies — validate our CURRENCY_MAP codes against NP ──────
    result["our_codes_check"] = check_our_currencies()

    return result


# ─────────────────────────────────────────
# Payouts (outbound crypto)
# ─────────────────────────────────────────

def _np_email() -> str:
    email = os.getenv("NOWPAYMENTS_EMAIL", "")
    if not email:
        raise ValueError("NOWPAYMENTS_EMAIL is not configured")
    return email


def _np_password() -> str:
    pwd = os.getenv("NOWPAYMENTS_PASSWORD", "")
    if not pwd:
        raise ValueError("NOWPAYMENTS_PASSWORD is not configured")
    return pwd


def _get_jwt_token() -> str:
    """
    POST /v1/auth → short-lived JWT for the Payouts API.
    Re-requested on every payout call; tokens are valid ~15 min.
    """
    resp = requests.post(
        f"{_BASE}/auth",
        json={"email": _np_email(), "password": _np_password()},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    logger.info("[NP] auth HTTP %d", resp.status_code)
    resp.raise_for_status()
    token = resp.json().get("token", "")
    if not token:
        raise ValueError(f"NowPayments /auth returned no token: {resp.text[:200]}")
    return token


def estimate_price(amount_usd, currency_to: str) -> Decimal:
    """
    GET /v1/estimate — convert USD amount to crypto equivalent.
    currency_to is a DB key (e.g. 'btc', 'usdttrc20').
    Returns the estimated crypto amount as Decimal.
    """
    np_code = to_np_code(currency_to)
    resp = requests.get(
        f"{_BASE}/estimate",
        params={"amount": float(amount_usd), "currency_from": "usd", "currency_to": np_code},
        headers={"x-api-key": _api_key()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    estimated = data.get("estimated_amount")
    if not estimated:
        raise ValueError(f"NP estimate returned no amount for {np_code}: {data}")
    logger.info("[NP] estimate $%s USD → %s %s", amount_usd, estimated, np_code.upper())
    return Decimal(str(estimated))


def create_payout(
    address: str,
    currency: str,
    amount_crypto: Decimal | float,
    withdrawal_id: int,
    callback_url: str,
) -> dict:
    """
    POST /v1/payout — send crypto from NowPayments balance to user address.

    currency is a DB key (e.g. 'btc'); translated to NP code via to_np_code().
    amount_crypto is the quantity in that crypto (NOT USD).

    Returns the NP response dict:
      { id (batch_id), status, withdrawals: [{id, status, address, currency, amount}] }
    """
    np_code     = to_np_code(currency)
    token       = _get_jwt_token()
    resolved_cb = _resolve_callback_url(callback_url)

    wd_entry: dict = {
        "address":  address,
        "currency": np_code,
        "amount":   float(amount_crypto),
    }
    if resolved_cb:
        wd_entry["ipn_callback_url"] = resolved_cb

    payload: dict = {"withdrawals": [wd_entry]}
    if resolved_cb:
        payload["ipn_callback_url"] = resolved_cb

    logger.info(
        "[NP] → POST /payout withdrawal_id=%d np_code=%s address=%s… amount=%s callback=%s",
        withdrawal_id, np_code, address[:14], amount_crypto,
        resolved_cb or "(omitted—localhost)",
    )
    resp = requests.post(
        f"{_BASE}/payout",
        headers={
            "x-api-key":     _api_key(),
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=30,
    )
    logger.info("[NP] ← payout HTTP %d body=%s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data      = resp.json()
    batch_wds = data.get("withdrawals", [])
    logger.info(
        "[NP] payout OK withdrawal_id=%d batch_id=%s payout_id=%s status=%s",
        withdrawal_id,
        data.get("id", "?"),
        batch_wds[0].get("id", "?") if batch_wds else "?",
        data.get("status", "?"),
    )
    return data


# ─────────────────────────────────────────
# IPN Signature Verification
# ─────────────────────────────────────────

def verify_ipn_signature(body_bytes: bytes, signature: str) -> bool:
    """
    Verify the x-nowpayments-sig HMAC-SHA512 signature sent with every IPN.

    NowPayments signs: JSON-encoded body with keys sorted alphabetically,
    no spaces, no trailing commas.

    Returns True only if the signature matches. Any exception → False.
    Rejects immediately if NOWPAYMENTS_IPN_SECRET is not set.
    """
    try:
        secret = _ipn_secret()
    except ValueError:
        logger.error("NOWPAYMENTS_IPN_SECRET not set — rejecting all IPNs")
        return False

    if not signature:
        return False

    try:
        payload   = json.loads(body_bytes)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        computed  = _hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        match = _hmac.compare_digest(computed, signature)
        if match:
            logger.debug("[NP] IPN signature OK")
        else:
            logger.warning(
                "[NP] IPN signature MISMATCH computed=%s… received=%s…",
                computed[:16], signature[:16],
            )
        return match
    except Exception as exc:
        logger.error("[NP] IPN signature verification error: %s", exc)
        return False
