# simulator/tests/test_dashboard_allowed_symbols.py
"""
Bloque A2 — UI/backend symbol sync.

Verifies that the rendered dashboard HTML only exposes symbols the backend
actually accepts (market_data.symbol_specs.allowed_symbols(), consumed by
consumers.py as _ALLOWED_SYMBOLS). Disabled-but-registered symbols (forex
minors, metals, indices, SOLUSD) must not appear in any symbol picker:
the desktop panel selector (SYMS), the quotes panel (ALL_SYMBOLS), the
mobile header selector (#mhdrSymSel), or the watchlist (#rcWatchlist).

LOT_SPECS is intentionally left untouched — it's a lookup table with a safe
default fallback ({step:0.01,min:0.01,dec:2}), not a symbol picker, and is
covered separately by test_lot_specs_frontend.py.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user
from market_data.symbol_specs import allowed_symbols

ENABLED_SYMBOLS = {"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "BTCUSD", "ETHUSD"}
DISABLED_SYMBOLS = [
    "USD/CAD", "USD/CHF", "NZD/USD", "SOLUSD",
    "XAU/USD", "XAG/USD", "US30", "US500", "NAS100",
]


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class DashboardAllowedSymbolsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_backend_allowed_symbols_matches_expected_enabled_set(self):
        # Guards the fixture list above against silent drift in symbol_specs.py.
        self.assertEqual(set(allowed_symbols()), ENABLED_SYMBOLS)

    def test_syms_array_only_contains_enabled_symbols(self):
        html = self._html()
        start = html.index("const SYMS=")
        end = html.index(";", start)
        syms_literal = html[start:end]
        for sym in ENABLED_SYMBOLS:
            self.assertIn(sym, syms_literal)
        for sym in DISABLED_SYMBOLS:
            self.assertNotIn(sym, syms_literal)

    def test_all_symbols_array_only_contains_enabled_symbols(self):
        html = self._html()
        start = html.index("const ALL_SYMBOLS=")
        end = html.index(";", start)
        literal = html[start:end]
        for sym in ENABLED_SYMBOLS:
            self.assertIn(sym, literal)
        for sym in DISABLED_SYMBOLS:
            self.assertNotIn(sym, literal)

    def test_mobile_header_select_only_contains_enabled_symbols(self):
        html = self._html()
        start = html.index('id="mhdrSymSel"')
        end = html.index("</select>", start)
        block = html[start:end]
        self.assertIn('value="EUR/USD"', block)
        self.assertIn('value="GBP/USD"', block)
        self.assertIn('value="USD/JPY"', block)
        self.assertIn('value="AUD/USD"', block)
        self.assertIn('value="BTCUSD"', block)
        self.assertIn('value="ETHUSD"', block)
        for sym in DISABLED_SYMBOLS:
            self.assertNotIn(f'value="{sym}"', block)

    def test_watchlist_only_contains_enabled_symbols(self):
        html = self._html()
        start = html.index('id="rcWatchlist"')
        end = html.index("</div>", start)
        block = html[start:end]
        for sym in DISABLED_SYMBOLS:
            self.assertNotIn(f'data-sym="{sym}"', block)
