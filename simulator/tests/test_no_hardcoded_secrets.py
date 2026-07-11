"""
simulator/tests/test_no_hardcoded_secrets.py — MD-1b

Regression guard for the hardcoded Finnhub token found in the old root-level
test_ws_finnhub.js (present since the repo's initial commit). The script was
moved to scripts/manual/ and rewritten to read FINNHUB_API_KEY from the
environment — this test locks that in so the literal token pattern can't
silently come back.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# A Finnhub token is a long alphanumeric string passed as ?token=...
_HARDCODED_TOKEN_RE = re.compile(r"token=[A-Za-z0-9]{20,}")


class TestFinnhubManualScriptHasNoHardcodedSecret(SimpleTestCase):
    def test_script_reads_key_from_environment(self):
        path = BASE_DIR / "scripts" / "manual" / "test_ws_finnhub.js"
        self.assertTrue(path.exists(), f"expected manual script at {path}")
        content = path.read_text()
        self.assertIn("process.env.FINNHUB_API_KEY", content)

    def test_script_has_no_literal_token(self):
        path = BASE_DIR / "scripts" / "manual" / "test_ws_finnhub.js"
        content = path.read_text()
        match = _HARDCODED_TOKEN_RE.search(content)
        self.assertIsNone(
            match,
            f"found a hardcoded-looking token in {path}: {match.group(0) if match else ''}",
        )

    def test_old_root_level_script_is_gone(self):
        old_path = BASE_DIR / "test_ws_finnhub.js"
        self.assertFalse(
            old_path.exists(),
            "the old root-level test_ws_finnhub.js (with the hardcoded token) "
            "should have been removed in favor of scripts/manual/test_ws_finnhub.js",
        )
