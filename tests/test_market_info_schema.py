import unittest
from unittest.mock import patch

import market


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return list(self._rows)


class MarketInfoSchemaTests(unittest.TestCase):
    def test_market_event_caution_schema_is_parsed(self):
        rows = [
            {
                "market": "KRW-ZRO",
                "korean_name": "레이어제로",
                "english_name": "LayerZero",
                "market_event": {
                    "warning": False,
                    "caution": {
                        "TRADING_VOLUME_SOARING": True,
                    },
                },
            },
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
                "market_event": {
                    "warning": False,
                    "caution": {
                        "TRADING_VOLUME_SOARING": False,
                    },
                },
            },
        ]
        with patch("market.requests.get", return_value=_FakeResponse(rows)):
            out = market.get_upbit_krw_markets(timeout_sec=1.0)
        self.assertEqual(out["KRW-ZRO"]["market_warning"], "CAUTION")
        self.assertEqual(out["KRW-BTC"]["market_warning"], "NONE")


if __name__ == "__main__":
    unittest.main()
