# simulator/currencies.py
"""
Single source of truth for crypto currency codes.

DB key  →  (NowPayments pay_currency code, human label)

Rules:
  - The DB key is what's stored in Deposit.crypto_currency.
  - The NP code is what's sent to POST /v1/payment as pay_currency.
  - When DB key == NP code (all cases below), no runtime translation is needed;
    the map is still the validation gate and the label registry.

To add a currency: append here only. Do NOT touch models.py or forms.py.
"""

CURRENCY_MAP: dict[str, tuple[str, str]] = {
    "btc":       ("btc",       "Bitcoin (BTC)"),
    "eth":       ("eth",       "Ethereum (ETH)"),
    "usdttrc20": ("usdttrc20", "USDT TRC-20"),
    "usdterc20": ("usdterc20", "USDT ERC-20"),
    "sol":       ("sol",       "Solana (SOL)"),
    "xrp":       ("xrp",       "XRP"),
    "bnbbsc":    ("bnbbsc",    "BNB (BEP-20)"),
    "usdcsol":   ("usdcsol",   "USDC (Solana)"),
}

# Django ChoiceField / model CharField choices — import in models.py / forms.py
CRYPTO_CHOICES: list[tuple[str, str]] = [(k, v[1]) for k, v in CURRENCY_MAP.items()]

# ── Withdrawal currencies (outbound payouts via NowPayments) ──────────────────
# Subset of CURRENCY_MAP; each entry must also be valid in CURRENCY_MAP.
WITHDRAWAL_CURRENCY_MAP: dict[str, tuple[str, str]] = {
    "btc":       CURRENCY_MAP["btc"],
    "eth":       CURRENCY_MAP["eth"],
    "usdttrc20": CURRENCY_MAP["usdttrc20"],
    "sol":       CURRENCY_MAP["sol"],
    "bnbbsc":    CURRENCY_MAP["bnbbsc"],
}

WITHDRAWAL_CHOICES: list[tuple[str, str]] = [(k, v[1]) for k, v in WITHDRAWAL_CURRENCY_MAP.items()]


def to_np_code(db_value: str) -> str:
    """
    Validate and return the NowPayments pay_currency code for a given DB key.
    Raises ValueError if the key is not in our supported list.
    """
    entry = CURRENCY_MAP.get(db_value.lower())
    if not entry:
        supported = ", ".join(CURRENCY_MAP.keys())
        raise ValueError(
            f"Currency '{db_value}' is not in our supported list. "
            f"Supported keys: {supported}"
        )
    return entry[0]  # NP code
