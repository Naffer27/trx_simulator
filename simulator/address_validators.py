# simulator/address_validators.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — local, network-aware address validation.

Pure stdlib (hashlib only — same primitive already used identically at
auth_password_views.py:45). No third-party dependency: none of base58,
bech32, coincurve, web3, eth-utils were installed in this project
(confirmed via pip list during the audit) and none is added here.

Scope, per Design Lock:
  - Real Base58Check decode + checksum for TRON mainnet (TRC20) and Bitcoin
    legacy/P2SH addresses.
  - Real Bech32 (BIP-173) / Bech32m (BIP-350) decode + checksum for Bitcoin
    SegWit v0 and Taproot addresses, mainnet ("bc" hrp) only.
  - Rejects testnet addresses of every format.
  - NO network calls of any kind (no blockchain RPC, no provider API) —
    purely local format/checksum validation. Whether NowPayments' payout
    actually accepts a given *valid* BTC format is a separate, unverified
    question (see WALLET_WITHDRAWAL_ENABLED_ASSETS / Design Lock Correction 1)
    — this module answers "is this address well-formed", nothing else.

The Bech32/Bech32m polymod algorithm below is the standard BIP-173/BIP-350
reference implementation (public domain), not a novel implementation —
reused verbatim as the well-tested minimal approach the Design Lock asked for.
"""
import hashlib
import hmac

# ─────────────────────────────────────────────
# Asset/network vocabulary for this block
# ─────────────────────────────────────────────
# Maps the existing single-code currencies.py DB key (already used by
# WithdrawalRequest.crypto_currency / WALLET_WITHDRAWAL_ENABLED_ASSETS) to
# the (asset, network) pair used by WithdrawalEmailOTPChallenge /
# VerifiedWithdrawalWallet (Design Lock sections C/D).
ASSET_NETWORK_BY_CURRENCY: dict[str, tuple[str, str]] = {
    "usdttrc20": ("USDT", "TRC20"),
    "btc":       ("BTC",  "BTC_MAINNET"),
}
CURRENCY_BY_ASSET_NETWORK: dict[tuple[str, str], str] = {
    v: k for k, v in ASSET_NETWORK_BY_CURRENCY.items()
}


# ─────────────────────────────────────────────
# Base58Check (TRC20 + BTC legacy/P2SH)
# ─────────────────────────────────────────────

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes | None:
    """Decode a Base58 string to raw bytes. Returns None if any char is invalid."""
    if not s or any(c not in _B58_ALPHABET for c in s):
        return None
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHABET.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    n_leading_zeros = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_leading_zeros + body


def _b58check_decode(s: str, expected_len: int = 25) -> bytes | None:
    """
    Decode + verify a Base58Check string (version byte + payload + 4-byte
    checksum). Returns (version byte + payload) on success, None otherwise.
    Checksum comparison is constant-time (hmac.compare_digest).
    """
    raw = _b58decode(s)
    if raw is None or len(raw) != expected_len:
        return None
    payload, checksum = raw[:-4], raw[-4:]
    computed = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if not hmac.compare_digest(checksum, computed):
        return None
    return payload


def validate_trc20_address(address: str) -> bool:
    """
    True iff *address* is a well-formed, checksum-valid TRON mainnet
    (USDT-TRC20) address: Base58Check, version byte 0x41 (the "T" prefix),
    21-byte payload (1 version + 20-byte hash) + 4-byte checksum = 25 bytes.
    """
    if not isinstance(address, str) or not address.startswith("T"):
        return False
    payload = _b58check_decode(address, expected_len=25)
    if payload is None:
        return False
    return payload[0] == 0x41


def validate_btc_base58_address(address: str) -> tuple[bool, str | None]:
    """Legacy P2PKH ('1...', version 0x00) or P2SH ('3...', version 0x05), mainnet only."""
    payload = _b58check_decode(address, expected_len=25)
    if payload is None:
        return False, None
    version = payload[0]
    if version == 0x00:
        return True, "P2PKH"
    if version == 0x05:
        return True, "P2SH"
    return False, None


# ─────────────────────────────────────────────
# Bech32 (BIP-173) / Bech32m (BIP-350) — reference algorithm, public domain
# ─────────────────────────────────────────────

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify_checksum(hrp, data) -> str | None:
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        return "bech32"
    if const == _BECH32M_CONST:
        return "bech32m"
    return None


def _bech32_decode_raw(bech: str):
    """Returns (hrp, data_words, spec) or (None, None, None)."""
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        return None, None, None
    if bech.lower() != bech and bech.upper() != bech:
        return None, None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return None, None, None
    if any(c not in _BECH32_CHARSET for c in bech[pos + 1:]):
        return None, None, None
    hrp = bech[:pos]
    data = [_BECH32_CHARSET.index(c) for c in bech[pos + 1:]]
    spec = _bech32_verify_checksum(hrp, data)
    if spec is None:
        return None, None, None
    return hrp, data[:-6], spec


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def validate_btc_segwit_address(address: str) -> tuple[bool, str | None]:
    """
    Bitcoin SegWit v0 ('bc1q...') or Taproot/v1+ ('bc1p...'), mainnet
    ("bc" hrp) only — rejects testnet ("tb" hrp) and any checksum/version
    mismatch (BIP-350: witness v0 must use Bech32, v1-16 must use Bech32m).
    """
    hrp, data, spec = _bech32_decode_raw(address)
    if hrp != "bc" or not data:
        return False, None
    witver = data[0]
    if not (0 <= witver <= 16):
        return False, None
    program = _convertbits(data[1:], 5, 8, False)
    if program is None or not (2 <= len(program) <= 40):
        return False, None
    if witver == 0:
        if spec != "bech32" or len(program) not in (20, 32):
            return False, None
        return True, "SEGWIT_V0"
    if spec != "bech32m":
        return False, None
    if witver == 1 and len(program) == 32:
        return True, "TAPROOT"
    return True, f"SEGWIT_V{witver}"


def validate_btc_address(address: str) -> tuple[bool, str | None]:
    """
    True + format label iff *address* is a well-formed, checksum-valid
    Bitcoin MAINNET address of any real format (legacy, P2SH, SegWit v0,
    Taproot). Rejects testnet addresses of every format.
    """
    if not isinstance(address, str) or not address:
        return False, None
    if address[0] in ("1", "3"):
        return validate_btc_base58_address(address)
    if address.lower().startswith(("bc1", "tb1")):
        return validate_btc_segwit_address(address)
    return False, None


# ─────────────────────────────────────────────
# Dispatch by currencies.py DB key
# ─────────────────────────────────────────────

def validate_address_for_currency(currency_key: str, address: str) -> bool:
    """
    True iff *address* is a well-formed, checksum-valid address for the
    given currencies.py DB key ("usdttrc20", "btc"). Unknown keys → False.
    """
    key = (currency_key or "").strip().lower()
    if key == "usdttrc20":
        return validate_trc20_address((address or "").strip())
    if key == "btc":
        return validate_btc_address((address or "").strip())[0]
    return False


def validate_address(asset: str, network: str, address: str) -> bool:
    """Same as validate_address_for_currency(), keyed by (asset, network) instead."""
    currency_key = CURRENCY_BY_ASSET_NETWORK.get(((asset or "").upper(), (network or "").upper()))
    if currency_key is None:
        return False
    return validate_address_for_currency(currency_key, address)
