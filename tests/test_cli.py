"""CLI smoke tests: every command must run against the shipped data."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from ticketedger.cli import main


def run(*argv: str):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class CliTests(unittest.TestCase):
    def test_summary_reports_all_offers(self):
        code, output = run("summary")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual({g["key"][0] for g in payload["offers"]}, {"P1", "P2", "P3"})
        self.assertIn("mature_basis", payload)

    def test_ledger_defaults_to_open_periods(self):
        code, output = run("ledger")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload)
        for period, body in payload.items():
            self.assertEqual(body["summary"]["state"], "open")

    def test_compare_returns_three_scenarios(self):
        code, output = run("compare")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(len(payload["scenarios"]), 3)
        self.assertEqual(len(payload["comparisons"]), 3)

    def test_bill_run_defaults_to_first_open_forward_month(self):
        code, output = run("bill")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["period"], "2027-01")
        self.assertEqual(
            payload["total_cents"],
            payload["variable_cents"] + payload["fixed_cents"],
        )

    def test_reconcile_lists_archive_and_suspense(self):
        code, output = run("reconcile")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(len(payload["archive"]), 3)
        self.assertTrue(all(item["matched"] for item in payload["archive"]))

    def test_trace_order(self):
        code, output = run("trace-order", "O012")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["order_id"], "O012")
        self.assertTrue(payload["refunds"])

    def test_trace_claim(self):
        code, output = run("trace-claim", "Q12")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["claim"], "Q12")
        self.assertIn("2026-11", payload["reclassed_in_periods"])

    def test_unknown_trace_target_exits_two(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, _ = run("trace-claim", "NOPE")
        self.assertEqual(code, 2)
        self.assertIn("NOPE", stderr.getvalue())

    def test_as_of_narrows_observation(self):
        _, full = run("summary")
        _, early = run("summary", "--as-of", "2026-11-23")
        full_payload = json.loads(full)
        early_payload = json.loads(early)
        full_p2 = next(g for g in full_payload["offers"] if g["key"][0] == "P2")
        early_p2 = next(g for g in early_payload["offers"] if g["key"][0] == "P2")
        self.assertLess(early_p2["recognized_cents"], full_p2["recognized_cents"])
        self.assertGreater(early_p2["refund_at_risk_cents"], 0)


if __name__ == "__main__":
    unittest.main()
