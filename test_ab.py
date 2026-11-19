import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("tracker_ab", Path(__file__).with_name("tracker.ab.py"))
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)
T = 1795094000


def trade(tx, offset, side="BUY", size=100, price="0.5", asset="123"):
    return dict(transaction_hash="0x" + tx, timestamp=T + offset, condition_id="0xcc",
                token_id=asset, side=side, size=size, price=price)


def page(rows, cursor=None):
    return dict(data=rows, pagination=dict(next_cursor=cursor, has_more=cursor is not None))


class Experiment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.db = ab.connect(self.path)
        self.addCleanup(lambda: self.db.close())
        ab.ingest(self.db, T, lambda *args, **kwargs: page([]))

    def poll(self, rows, offset=200):
        return ab.ingest(self.db, T + offset, lambda *args, **kwargs: page(rows))

    def cash(self):
        return dict(self.db.execute("select * from accounts"))

    def test_partial_sell_and_binary_settlement_survive_restart(self):
        buy = trade("a", 1, size=10000)
        sale = trade("b", 2, "SELL", 2500, "0.8")
        self.poll([sale, buy, buy, trade("old", 0)])
        self.assertIn("realized paper P&L $+0.75", ab.report(self.db))
        self.assertIn("realized paper P&L $+1.50", ab.report(self.db))
        self.db.close()
        self.db = ab.connect(self.path)
        self.poll([sale, buy], 230)
        self.assertEqual(self.db.execute("select count(*) from trades").fetchone()[0], 2)
        market = dict(condition_id="0xcc", closed=True, tokens=[
            dict(token_id="123", winner=True), dict(token_id="124", winner=False)])
        ab.settle(self.db, market, "0xcc")
        ab.settle(self.db, market, "0xcc")
        self.assertEqual({name: ab.Decimal(cash) for name, cash in self.cash().items()},
                         {"A": ab.Decimal("1004.50"), "B": ab.Decimal("1009.00")})
        self.assertEqual(self.db.execute("select count(*) from holdings").fetchone()[0], 0)

    def test_insufficient_cash_unmatched_sell_and_losing_settlement(self):
        self.poll([trade("large", 2, size=3000000), trade("old_sale", 1, "SELL")])
        self.assertEqual(self.cash(), {"A": "1000", "B": "990"})
        tokens = [dict(token_id="123", winner=False), dict(token_id="124", winner=False)]
        market = dict(condition_id="0xcc", closed=True, tokens=tokens)
        ab.settle(self.db, market, "0xcc")
        self.assertEqual(self.db.execute("select count(*) from holdings").fetchone()[0], 1)
        tokens[1]["winner"] = True
        ab.settle(self.db, market, "0xcc")
        self.assertIn("realized paper P&L $-10.00", ab.report(self.db))

    def test_late_trade_refuses_batch(self):
        self.poll([trade("a", 2)])
        with self.assertRaisesRegex(ValueError, "Late unseen"):
            self.poll([trade("b", 100), trade("a", 2), trade("late", 1)], 230)
        self.assertEqual(ab.setting(self.db, "checkpoint"), T + 80)
        self.assertEqual(self.db.execute("select count(*) from trades").fetchone()[0], 1)

    def test_cursor_and_same_second_order(self):
        first, second = trade("same", 2), trade("same", 2, "SELL")
        def get(url, params):
            return page([second], "next") if "cursor" not in params else page([first])
        rows = ab.fetch(T + 1, get)
        self.assertEqual([row.side for row in rows], ["BUY", "SELL"])
        self.assertNotEqual(rows[0].id, rows[1].id)
        with self.assertRaisesRegex(ValueError, "repeated"):
            ab.fetch(T + 1, lambda *args, **kwargs: page([first], "loop"))

    def test_timestamp_and_dedup_normalization(self):
        row = trade("same", 2)
        alternate = dict(row, timestamp=row["timestamp"] * 1000, price="0.500", size="100.0")
        self.assertEqual(ab.Trade.parse(row), ab.Trade.parse(alternate))
        self.assertNotEqual(ab.Trade.parse(dict(row, log_index=0)).id,
                            ab.Trade.parse(dict(row, log_index=1)).id)


if __name__ == "__main__":
    unittest.main()
