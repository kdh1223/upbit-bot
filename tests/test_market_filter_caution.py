import unittest

import config
import market


_MISSING = object()


class MarketFilterCautionTests(unittest.TestCase):
    def setUp(self):
        self._backup = {}
        self._set("EXCLUDE_CAUTION", True)

    def tearDown(self):
        for name, original in self._backup.items():
            if original is _MISSING:
                try:
                    delattr(config, name)
                except AttributeError:
                    pass
            else:
                setattr(config, name, original)

    def _set(self, name, value):
        if name not in self._backup:
            self._backup[name] = getattr(config, name, _MISSING)
        setattr(config, name, value)

    def test_blocks_caution_when_market_info_available(self):
        active, inactive, reasons = market.filter_tradeable_tickers(
            ["KRW-ZRO"],
            {"KRW-ZRO": {"market_warning": "CAUTION"}},
            strict_registry=True,
        )
        self.assertEqual(active, [])
        self.assertEqual(inactive, ["KRW-ZRO"])
        self.assertEqual(reasons.get("KRW-ZRO"), "CAUTION")

    def test_blocks_entry_when_market_info_missing_in_strict_mode(self):
        active, inactive, reasons = market.filter_tradeable_tickers(
            ["KRW-ZRO"],
            {},
            strict_registry=True,
        )
        self.assertEqual(active, [])
        self.assertEqual(inactive, ["KRW-ZRO"])
        self.assertEqual(reasons.get("KRW-ZRO"), "MARKET_INFO_UNAVAILABLE")

    def test_allows_entry_when_market_info_missing_in_non_strict_mode(self):
        active, inactive, reasons = market.filter_tradeable_tickers(
            ["KRW-ZRO"],
            {},
            strict_registry=False,
        )
        self.assertEqual(active, ["KRW-ZRO"])
        self.assertEqual(inactive, [])
        self.assertEqual(reasons, {})


if __name__ == "__main__":
    unittest.main()
