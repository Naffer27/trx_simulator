# simulator/tests/test_dead_trading_code_removed.py
"""
INTERNAL-BROKER-TRADING-CERTIFICATION-01C — dead trading code cleanup.

Regression coverage proving the two confirmed-dead findings from the
preflight audit stay removed:

  1. simulator/order_engine.py — deleted (zero imports/callers since the
     initial commit; its PnL/margin formulas omitted contract_size
     entirely, the same bug class as historical FIX-01/the admin margin
     display bug, and were never live).
  2. dashboard.html::_recomputeSpread() — deleted along with its call
     site and the this.spread=0 initializer. It was invoked every panel
     reset but the value it computed (this.spread) was never read
     anywhere else in the template — a pure no-op, confirmed removable
     with zero behavior change.

Neither removal touches any live formula (pnl_engine.py, consumers.py,
spread_engine.py) or any other dashboard.html behavior.
"""
import os

from django.test import TestCase


class OrderEngineFileRemovedTests(TestCase):
    def test_order_engine_module_file_does_not_exist(self):
        path = os.path.join(os.path.dirname(__file__), "..", "order_engine.py")
        self.assertFalse(
            os.path.exists(path),
            "simulator/order_engine.py should be deleted (dead code, zero callers)",
        )

    def test_order_engine_not_importable(self):
        with self.assertRaises(ImportError):
            import simulator.order_engine  # noqa: F401

    def test_no_order_engine_reference_anywhere_in_source(self):
        """Scoped to source directories only — docs/ legitimately
        discusses "order_engine.py" as prose in audit deliverables
        (INTERNAL-BROKER-TRADING-CERTIFICATION-01/01C), which is not a
        code reference and must not fail this check."""
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        source_dirs = ["simulator", "market_data", "trx_simulator"]
        hits = []
        for source_dir in source_dirs:
            walk_root = os.path.join(repo_root, source_dir)
            if not os.path.isdir(walk_root):
                continue
            for dirpath, dirnames, filenames in os.walk(walk_root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in (".git", ".venv", "node_modules", "__pycache__")
                ]
                for fname in filenames:
                    if not fname.endswith((".py", ".html", ".js")):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    if fpath.endswith(os.path.join("simulator", "tests", "test_dead_trading_code_removed.py")):
                        continue  # this file itself legitimately names the strings
                    try:
                        with open(fpath, encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                    except OSError:
                        continue
                    if "order_engine" in content or "OrderEngine" in content:
                        hits.append(fpath)
        self.assertEqual(hits, [], f"Unexpected order_engine reference(s): {hits}")


class RecomputeSpreadRemovedTests(TestCase):
    def _template_source(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "templates", "simulator", "dashboard.html",
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_recompute_spread_method_removed(self):
        self.assertNotIn("_recomputeSpread", self._template_source())

    def test_this_spread_removed(self):
        self.assertNotIn("this.spread", self._template_source())
