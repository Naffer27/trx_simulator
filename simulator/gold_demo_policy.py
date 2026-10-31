"""Fail-closed, account-scoped gold pilot. Never changes the global registry."""
import os


def gold_demo_allowed(account_id, account_type, currency='USD', allowed_symbols=None):
    if os.getenv('MASSIVE_GOLD_DATA_ENABLED', '').strip() != '1':
        return False
    if account_type != 'DEMO' or currency != 'USD' or not account_id:
        return False
    raw = os.getenv('GOLD_DEMO_ACCOUNT_IDS', '').strip()
    parts = raw.split(',') if raw else []
    if not parts or any(not part.strip().isdigit() or int(part.strip()) <= 0 for part in parts):
        return False
    if allowed_symbols is not None and 'XAU/USD' not in allowed_symbols:
        return False
    return str(account_id) in {str(int(part.strip())) for part in parts}


def gold_demo_for_account(account):
    return gold_demo_allowed(account.pk, account.account_type, account.currency,
                             account.allowed_symbols_snapshot)
