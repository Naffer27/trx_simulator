# simulator/tests/test_contract_size_ssot.py
"""
INTERNAL-BROKER-TRADING-CERTIFICATION-01B — Contract Size Single Source
of Truth.

Regression coverage for the removal of dashboard.html's manually-
maintained CONTRACT_SIZE object literal. The dashboard now renders
contract_size_json (simulator/views.py::trading_dashboard(), built from
market_data.symbol_specs.get_all_specs() — the same authoritative
registry pnl_engine.py/risk_engine.py use for live margin/PnL) straight
into `const CONTRACT_SIZE = {{ contract_size_json|safe }};`, following
the exact existing pattern equity_curve_json/closed_trades_json already
use. No formula changed anywhere — only where the JS constant's values
originate.

Covers:
  A. Dashboard context's contract_size_json exactly equals
     {sp.symbol: sp.contract_size for sp in get_all_specs()}.
  B. Rendered dashboard contains the authoritative values for
     EUR/USD, BTCUSD, ETHUSD, XAU/USD.
  C. The old manually-maintained multi-line CONTRACT_SIZE object body
     (one "SYM": value pair per line) no longer exists in the template
     source.
  D. getContractSize() still reads from the injected CONTRACT_SIZE
     constant (unchanged function body).
"""
import json

from django.test import TestCase
from django.urls import reverse

from market_data.symbol_specs import get_all_specs
from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class ContractSizeContextMappingTests(TestCase):
    """A — the JSON context value exactly matches the backend registry."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def test_context_contract_size_json_matches_get_all_specs(self):
        resp = self.client.get(_url(self.account.pk))
        self.assertEqual(resp.status_code, 200)
        rendered = json.loads(resp.context["contract_size_json"])
        expected = {sp.symbol: sp.contract_size for sp in get_all_specs()}
        self.assertEqual(rendered, expected)

    def test_context_mapping_is_not_empty_and_covers_enabled_symbols(self):
        resp = self.client.get(_url(self.account.pk))
        rendered = json.loads(resp.context["contract_size_json"])
        for sym in ("EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "BTCUSD", "ETHUSD"):
            self.assertIn(sym, rendered)


class RenderedDashboardAuthoritativeValuesTests(TestCase):
    """B — the rendered HTML's CONTRACT_SIZE literal carries the correct
    authoritative numbers for the symbols the task named."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _contract_size_block(self):
        resp = self.client.get(_url(self.account.pk))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        start = html.index("const CONTRACT_SIZE")
        end = html.index("};", start)
        return html[start:end]

    def test_eurusd_100000(self):
        self.assertIn('"EUR/USD": 100000', self._contract_size_block())

    def test_btcusd_1(self):
        # SymbolSpec.contract_size for BTCUSD is the float 1.0 — json.dumps
        # renders "1.0", numerically identical to the old hand-typed "1".
        self.assertIn('"BTCUSD": 1.0', self._contract_size_block())

    def test_ethusd_1(self):
        self.assertIn('"ETHUSD": 1.0', self._contract_size_block())

    def test_xauusd_100(self):
        self.assertIn('"XAU/USD": 100.0', self._contract_size_block())


class OldHardcodedTableRemovedTests(TestCase):
    """C — the old manually-maintained, one-pair-per-line object body is
    gone from the template source (not merely from the rendered output —
    checked against the raw template file so a future edit can't
    reintroduce a parallel hardcoded copy elsewhere)."""

    def test_template_source_has_no_manual_per_symbol_lines(self):
        with open(
            "simulator/templates/simulator/dashboard.html", encoding="utf-8"
        ) as f:
            src = f.read()
        self.assertNotIn('"EUR/USD": 100000,\n', src)
        self.assertNotIn('"GBP/USD": 100000,\n', src)
        self.assertNotIn('"BTCUSD":  1,\n', src)
        # The single injected-assignment line must be present instead.
        self.assertIn(
            "const CONTRACT_SIZE = {{ contract_size_json|safe }};", src
        )


class GetContractSizeConsumesInjectedMappingTests(TestCase):
    """D — getContractSize() itself is unchanged and reads off whatever
    CONTRACT_SIZE resolves to (now the injected mapping)."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def test_get_contract_size_function_present_and_reads_the_constant(self):
        resp = self.client.get(_url(self.account.pk))
        html = resp.content.decode()
        self.assertIn(
            "function getContractSize(sym){ return CONTRACT_SIZE[sym] ?? 1; }",
            html,
        )
