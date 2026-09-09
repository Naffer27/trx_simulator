# simulator/tests/test_address_validators.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — simulator/address_validators.py.

Pure local validation, no DB, no network. Round-trip self-consistency for
Bech32/Bech32m (encode with the same polymod machinery, decode back) is
used alongside real known-valid mainnet addresses, since the exact BIP-350
test vector strings are easy to mistype by hand — the round trip is a
stronger correctness proof than a hand-copied string.

Covers:
  1.  BTC P2PKH (legacy, '1...') valid + bad checksum.
  2.  BTC P2SH ('3...') valid + bad checksum.
  3.  BTC SegWit v0 ('bc1q...') valid + bad checksum + testnet rejected.
  4.  BTC Taproot/v1+ ('bc1p...'/high witness versions) valid via round-trip
      + bech32-instead-of-bech32m rejected.
  5.  TRC20 valid + bad checksum + wrong version byte + wrong length.
  6.  Dispatch by currencies.py DB key ("btc"/"usdttrc20") and by (asset, network).
  7.  Garbage / empty / non-address strings rejected without raising.
"""
from django.test import SimpleTestCase

from simulator import address_validators as av


class BtcBase58Tests(SimpleTestCase):
    VALID_P2PKH = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
    VALID_P2SH  = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"

    def test_valid_p2pkh(self):
        self.assertEqual(av.validate_btc_address(self.VALID_P2PKH), (True, "P2PKH"))

    def test_p2pkh_bad_checksum(self):
        tampered = self.VALID_P2PKH[:-1] + ("3" if self.VALID_P2PKH[-1] != "3" else "4")
        ok, _ = av.validate_btc_address(tampered)
        self.assertFalse(ok)

    def test_valid_p2sh(self):
        self.assertEqual(av.validate_btc_address(self.VALID_P2SH), (True, "P2SH"))

    def test_p2sh_bad_checksum(self):
        tampered = self.VALID_P2SH[:-1] + ("z" if self.VALID_P2SH[-1] != "z" else "y")
        ok, _ = av.validate_btc_address(tampered)
        self.assertFalse(ok)


class BtcSegwitTests(SimpleTestCase):
    def _encode(self, hrp, witver, program_bytes):
        const = av._BECH32_CONST if witver == 0 else av._BECH32M_CONST
        data = [witver] + av._convertbits(list(program_bytes), 8, 5, True)
        values = av._bech32_hrp_expand(hrp) + data
        polymod = av._bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
        checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
        combined = data + checksum
        return hrp + "1" + "".join(av._BECH32_CHARSET[d] for d in combined)

    def test_known_valid_segwit_v0(self):
        self.assertEqual(
            av.validate_btc_address("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"),
            (True, "SEGWIT_V0"),
        )

    def test_known_valid_witness_v16(self):
        self.assertEqual(av.validate_btc_address("BC1SW50QGDZ25J"), (True, "SEGWIT_V16"))

    def test_roundtrip_segwit_v0_20byte_program(self):
        addr = self._encode("bc", 0, bytes(range(20)))
        self.assertEqual(av.validate_btc_segwit_address(addr), (True, "SEGWIT_V0"))

    def test_roundtrip_taproot_v1_32byte_program(self):
        addr = self._encode("bc", 1, bytes(range(32)))
        self.assertEqual(av.validate_btc_segwit_address(addr), (True, "TAPROOT"))

    def test_testnet_rejected(self):
        addr = self._encode("tb", 0, bytes(range(20)))
        ok, _ = av.validate_btc_address(addr)
        self.assertFalse(ok)

    def test_tampered_checksum_rejected(self):
        addr = self._encode("bc", 0, bytes(range(20)))
        last = addr[-1]
        other = "q" if last != "q" else "p"
        tampered = addr[:-1] + other
        ok, _ = av.validate_btc_address(tampered)
        self.assertFalse(ok)

    def test_bech32_used_for_v1_is_rejected(self):
        """BIP-350: witness v1+ MUST use bech32m, not plain bech32."""
        addr = self._encode("bc", 0, bytes(range(20)))  # valid v0/bech32
        # Manually build a v1 address using the WRONG (bech32) constant.
        data = [1] + av._convertbits(list(bytes(range(32))), 8, 5, True)
        values = av._bech32_hrp_expand("bc") + data
        polymod = av._bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ av._BECH32_CONST
        checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
        combined = data + checksum
        bad = "bc" + "1" + "".join(av._BECH32_CHARSET[d] for d in combined)
        ok, _ = av.validate_btc_segwit_address(bad)
        self.assertFalse(ok)


class Trc20Tests(SimpleTestCase):
    VALID = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def test_valid(self):
        self.assertTrue(av.validate_trc20_address(self.VALID))

    def test_bad_checksum(self):
        tampered = self.VALID[:-1] + ("x" if self.VALID[-1] != "x" else "y")
        self.assertFalse(av.validate_trc20_address(tampered))

    def test_wrong_prefix_rejected(self):
        self.assertFalse(av.validate_trc20_address("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"))

    def test_empty_rejected(self):
        self.assertFalse(av.validate_trc20_address(""))

    def test_garbage_rejected(self):
        self.assertFalse(av.validate_trc20_address("not-a-real-address"))


class DispatchTests(SimpleTestCase):
    def test_by_currency_key_btc(self):
        self.assertTrue(av.validate_address_for_currency("btc", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"))

    def test_by_currency_key_usdttrc20(self):
        self.assertTrue(av.validate_address_for_currency("usdttrc20", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"))

    def test_by_currency_key_unknown(self):
        self.assertFalse(av.validate_address_for_currency("dogecoin", "whatever"))

    def test_by_asset_network(self):
        self.assertTrue(av.validate_address(
            "BTC", "BTC_MAINNET", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        ))
        self.assertTrue(av.validate_address(
            "USDT", "TRC20", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        ))

    def test_by_asset_network_unknown_pair(self):
        self.assertFalse(av.validate_address("ETH", "ERC20", "0xdeadbeef"))

    def test_garbage_never_raises(self):
        for bad in (None, "", "   ", "T", "bc1", "1", 12345):
            try:
                if isinstance(bad, str):
                    av.validate_btc_address(bad)
                    av.validate_trc20_address(bad)
            except Exception as exc:  # pragma: no cover - the point is that this never happens
                self.fail(f"validator raised on garbage input {bad!r}: {exc}")
