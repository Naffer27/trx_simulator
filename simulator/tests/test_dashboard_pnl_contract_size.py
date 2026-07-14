# simulator/tests/test_dashboard_pnl_contract_size.py
"""
Bloque C1 — dashboard-position-pnl-contract-size-fix.

Verifies (white-box, HTML source inspection — same approach as
test_lot_specs_frontend.py and test_dashboard_allowed_symbols.py):

  1. A CONTRACT_SIZE map exists in the rendered dashboard, with correct
     values for the 6 currently-enabled symbols (EUR/USD, GBP/USD,
     USD/JPY, AUD/USD = 100000; BTCUSD, ETHUSD = 1).
  2. XAU/USD may exist in CONTRACT_SIZE as internal metadata (contract_size
     mirrors the backend spec) but must NOT appear in any visible/selectable
     symbol picker (SYMS, ALL_SYMBOLS, #mhdrSymSel, #rcWatchlist) — gold
     stays disabled.
  3. Every per-position PnL calculation site multiplies by contract size
     (either via the `cs`/getContractSize() local or literally
     `CONTRACT_SIZE[sym]`), not the old bare `(px-entry)*qty` formula.
  4. Account-level PnL rendering is untouched — still reads msg.upnl /
     msg.pnl_unreal, which the backend already computes with contract_size.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

DISABLED_SYMBOLS = [
    "USD/CAD", "USD/CHF", "NZD/USD", "SOLUSD",
    "XAU/USD", "XAG/USD", "US30", "US500", "NAS100",
]


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class DashboardPnLContractSizeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _contract_size_block(self, html):
        start = html.index("const CONTRACT_SIZE")
        end = html.index("};", start)
        return html[start:end]

    # ── CONTRACT_SIZE map ────────────────────────────────────────────────

    def test_contract_size_map_present(self):
        self.assertIn("const CONTRACT_SIZE", self._html())

    def test_eurusd_contract_size_100000(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"EUR/USD": 100000', block)

    def test_gbpusd_contract_size_100000(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"GBP/USD": 100000', block)

    def test_usdjpy_contract_size_100000(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"USD/JPY": 100000', block)

    def test_audusd_contract_size_100000(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"AUD/USD": 100000', block)

    def test_btcusd_contract_size_1(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"BTCUSD":  1', block)

    def test_ethusd_contract_size_1(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"ETHUSD":  1', block)

    def test_get_contract_size_helper_present(self):
        self.assertIn("function getContractSize(sym)", self._html())

    # ── XAU/USD: metadata-only, never a visible selector option ─────────

    def test_xauusd_may_be_metadata_in_contract_size(self):
        block = self._contract_size_block(self._html())
        self.assertIn('"XAU/USD": 100', block)

    def test_xauusd_not_in_syms_array(self):
        html = self._html()
        start = html.index("const SYMS=")
        end = html.index(";", start)
        self.assertNotIn("XAU/USD", html[start:end])

    def test_xauusd_not_in_all_symbols_array(self):
        html = self._html()
        start = html.index("const ALL_SYMBOLS=")
        end = html.index(";", start)
        self.assertNotIn("XAU/USD", html[start:end])

    def test_xauusd_not_in_mobile_header_select(self):
        html = self._html()
        start = html.index('id="mhdrSymSel"')
        end = html.index("</select>", start)
        self.assertNotIn('value="XAU/USD"', html[start:end])

    def test_xauusd_not_in_watchlist(self):
        html = self._html()
        start = html.index('id="rcWatchlist"')
        end = html.index("</div>", start)
        self.assertNotIn('data-sym="XAU/USD"', html[start:end])

    def test_no_disabled_symbol_selectable_anywhere(self):
        """Belt-and-suspenders: none of the disabled symbols appear as a
        selectable option value or watchlist entry anywhere in the page."""
        html = self._html()
        for sym in DISABLED_SYMBOLS:
            self.assertNotIn(f'value="{sym}"', html)
            self.assertNotIn(f'data-sym="{sym}"', html)

    # ── Per-position PnL sites use contract size ─────────────────────────
    # MARGIN-02: every site below now delegates to computePositionPnL()/
    # computeRawPnL() (simulator/pnl_engine.py's client-side mirror) instead
    # of inlining `getContractSize(...)*cs` — those two shared helpers are
    # themselves verified (once) to multiply by contract size in
    # test_shared_pnl_helpers_use_contract_size below, so every call site
    # inherits that guarantee without repeating the formula.

    def test_shared_pnl_helpers_use_contract_size(self):
        html = self._html()
        start = html.index("function computeRawPnL")
        end = html.index("\n}", start)
        block = html[start:end]
        self.assertIn("getContractSize(", block)
        self.assertIn("*cs", block)

    def test_render_global_positions_uses_contract_size(self):
        html = self._html()
        self.assertIn("function renderGlobalPositions", html)
        self.assertIn("computePositionPnL(r, px)", html)

    def test_trading_tab_position_list_uses_contract_size(self):
        html = self._html()
        self.assertIn("computePositionPnL(pos, px)", html)

    def test_patch_trading_panel_pnl_uses_contract_size(self):
        html = self._html()
        start = html.index("function _patchTradingPanelPnL")
        end = html.index("\n}", start)
        block = html[start:end]
        self.assertIn("computePositionPnL(pos, px)", block)

    def test_quick_close_sheet_uses_contract_size(self):
        html = self._html()
        start = html.index("function openSheet(focus)")
        end = html.index("\n}", start)
        block = html[start:end]
        self.assertIn("computePositionPnL(pos, px)", block)
        self.assertIn("computeRawPnL(", block)

    def test_no_bare_price_delta_times_qty_without_contract_size(self):
        """Guards against regressing back to `(px-entry)*qty` without `*cs`."""
        html = self._html()
        self.assertNotIn("(px-entry)*qty:(entry-px)*qty", html)
        self.assertNotIn("(px-rec.entry)*qty:(rec.entry-px)*qty", html)

    # ── Account-level PnL untouched ──────────────────────────────────────

    def test_account_level_pnl_still_reads_backend_upnl(self):
        html = self._html()
        self.assertIn("msg.upnl??msg.pnl_unreal", html)
