import csv
from pathlib import Path
import unittest

DATA = Path(__file__).resolve().parents[1] / "data"

TABLES = {
    'offers': ('offer component weight supplier refundable cost_cents valid_from valid_through', 'offer component'),
    'orders': ('order_id offer household channel purchase_date normal_window_end', 'order_id'),
    'vouchers': ('coupon_id order_id component supplier guest previous scheduled redeemed state', 'coupon_id'),
    'money': ('entry_id order_id kind amount_cents component claim funder event_date received_date', 'entry_id'),
    'disputes': ('receipt claim order_id component resolved_cents result replacement_of latest_resolution recorded_at', 'receipt'),
    'books': ('period state locked_on', 'period'),
    'book_entries': ('book_id period source entry_scope posted_cents', 'book_id'),
    'supplier_terms': ('term_id supplier component fixed_cents avoidable_if_stopped cancelled_on', 'term_id'),
    'commercial_actions': ('action_id offer replacement state event_date', 'action_id'),
}


class SourceDataTests(unittest.TestCase):
    def load(self, name):
        fields, _ = TABLES[name]
        with (DATA / (name + ".csv")).open(encoding="utf-8", newline="") as handle:
            contents = csv.DictReader(handle)
            self.assertEqual(contents.fieldnames, fields.split())
            records = list(contents)
        self.assertTrue(records)
        self.assertTrue(all(None not in row and None not in row.values() for row in records))
        return records

    def test_format_and_record_keys(self):
        for name, (_, key) in TABLES.items():
            with self.subTest(table=name):
                records = self.load(name)
                values = [tuple(row[field] for field in key.split()) for row in records]
                self.assertTrue(all(all(value) for value in values))
                self.assertEqual(len(values), len(set(values)))

    def test_related_source_records(self):
        rows = {name: self.load(name) for name in TABLES}
        coupons = {r["coupon_id"]: r for r in rows["vouchers"]}
        self.assertTrue(any(r["previous"] in coupons and
                            coupons[r["previous"]]["supplier"] != r["supplier"]
                            for r in coupons.values()))
        self.assertTrue(any(not r["component"] for r in rows["money"] if r["kind"] == "refund"))
        self.assertTrue(any(r["replacement_of"] for r in rows["disputes"]))
        self.assertEqual({"closed", "open"}, {r["state"] for r in rows["books"]})
