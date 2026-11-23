"""Behavioural tests for the operating and financial views.

Each test pins one rule from the brief so a future change cannot silently
break it.  The tests use the shipped synthetic data as a fixture and assert on
reconciled totals, not on incidental formatting.
"""

from __future__ import annotations

import unittest
from datetime import date

from ticketedger import FinancialLedger, build_economic_view, load_dataset
from ticketedger.allocation import UNALLOCATED, allocate_weighted
from ticketedger.billing import build_bill_run, next_bill_period
from ticketedger.financial import period_of
from ticketedger.refunds import resolve_refunds
from ticketedger.scenario import (
    allocated_incurred_fixed,
    compare_scenarios,
    decision_table,
    fixed_plan,
    incurred_fixed,
    open_suspense_claims,
)
from ticketedger.trace import trace_claim, trace_order

AS_OF = date(2026, 11, 23)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ds = load_dataset()
        cls.view = build_economic_view(cls.ds, AS_OF)
        cls.ledger = FinancialLedger(cls.ds, cls.view, AS_OF)


class AllocationTests(Fixture):
    def test_weighted_split_is_exact_and_deterministic(self):
        split = allocate_weighted(20200, [("hall", 50), ("lab", 30), ("bus", 20)])
        self.assertEqual(sum(split.values()), 20200)
        self.assertEqual(split, {"hall": 10100, "lab": 6060, "bus": 4040})
        tricky = allocate_weighted(100, [("a", 1), ("b", 1), ("c", 1)])
        self.assertEqual(sum(tricky.values()), 100)

    def test_every_order_pool_and_net_reconcile(self):
        for order in self.view.orders:
            components = [
                c for c in order.components if c.component != UNALLOCATED
            ]
            with self.subTest(order=order.order.order_id):
                self.assertEqual(
                    sum(c.gross_cents for c in components),
                    order.distributor_pool_cents,
                )
                self.assertEqual(
                    order.net_cents
                    + sum(c.adjustment_cents for c in components)
                    + sum(c.refund_cents for c in components),
                    order.distributor_pool_cents,
                )

    def test_issuer_discount_reduces_pool_but_platform_does_not(self):
        # O003 carries an issuer-funded 800 coupon; O012 a platform 1500 one.
        issuer_order = self.view.order_line("O003")
        platform_order = self.view.order_line("O012")
        self.assertEqual(issuer_order.issuer_discount_cents, 800)
        self.assertEqual(
            issuer_order.distributor_pool_cents,
            issuer_order.capture_cents - 800,
        )
        self.assertEqual(platform_order.issuer_discount_cents, 0)
        self.assertEqual(platform_order.platform_discount_cents, 1500)
        self.assertEqual(
            platform_order.distributor_pool_cents, platform_order.capture_cents
        )

    def test_platform_subsidy_is_memo_not_netted_twice(self):
        # The platform-funded coupon on O012 must not reduce its pool.
        order = self.view.order_line("O012")
        self.assertEqual(order.platform_discount_cents, 1500)
        self.assertEqual(order.distributor_pool_cents, 20200)
        for line in self.ledger.lines:
            if line.account == "platform_subsidy":
                self.assertTrue(line.memo)
                self.assertEqual(line.effective_cents, 0)

    def test_void_voucher_earns_nothing_and_successor_does(self):
        order = self.view.order_line("O012")
        lab = next(c for c in order.components if c.component == "lab")
        self.assertEqual(lab.state, "redeemed")
        self.assertEqual(lab.supplier, "D")  # re-issued to a different supplier
        self.assertEqual(lab.sold_supplier, "B")
        self.assertTrue(lab.supplier_changed)
        self.assertEqual(lab.voucher_chain, ("V012-lab", "VR12"))
        self.assertGreater(lab.recognized_cents, 0)
        self.assertEqual(lab.rate_cents, self.ds.rate_for("P1", "lab", "D"))

    def test_unredeemed_component_is_deferred_not_revenue(self):
        for line in self.view.component_lines():
            if line.component == UNALLOCATED:
                continue
            with self.subTest(order=line.order_id, component=line.component):
                if line.state == "unused":
                    self.assertEqual(line.recognized_cents, 0)
                    self.assertEqual(line.deferred_cents, line.net_cents)
                elif line.redeemed_state:
                    self.assertEqual(line.deferred_cents, 0)

    def test_recognised_revenue_equals_sum_of_delivered_components(self):
        total = sum(
            line.recognized_cents
            for line in self.view.component_lines()
            if line.component != UNALLOCATED
        )
        self.assertEqual(
            total, sum(group.recognized_cents for group in self.view.offer_summary())
        )

    def test_supplier_change_prices_actual_deliverer(self):
        lab = self.view.component_line("O012", "lab")
        self.assertEqual(lab.supplier, "D")
        # P1's own B rate is 4700; D's rate is 3900.
        self.assertEqual(lab.variable_cost_cents, 3900)


class RefundTests(Fixture):
    def test_replacement_receipt_is_not_a_second_refund(self):
        allocation = self.view.refund_for("Q12")
        self.assertEqual(allocation.cash_cents, 6500)
        self.assertEqual(allocation.allocated_cents, 6500)
        self.assertEqual(allocation.component_cents("bus"), 2500)  # D2, not D1+D2
        self.assertEqual(allocation.component_cents("lab"), 4000)
        self.assertEqual(allocation.unallocated_cents, 0)
        self.assertEqual(
            sorted(a.receipt for a in allocation.receipts if a.is_final),
            ["D2", "D3"],
        )
        d1 = next(a for a in allocation.receipts if a.receipt == "D1")
        self.assertFalse(d1.is_final)
        self.assertEqual(d1.superseded_by, ("D2",))

    def test_blank_component_uses_receipts_not_nearest_redemption(self):
        allocation = self.view.refund_for("Q12")
        # bus was never redeemed on O012; the cash must still land on bus via D2.
        self.assertEqual(allocation.components["bus"].basis, "receipt")
        self.assertFalse(
            any(line.refund_cents for line in self.view.order_line("O012").components
                if line.component == "hall")
        )

    def test_explicit_channel_component_is_recognised_as_weaker_evidence(self):
        allocation = self.view.refund_for("Q46")
        self.assertEqual(allocation.allocated_cents, 3100)
        self.assertEqual(allocation.components["bus"].basis, "channel")
        self.assertEqual(
            allocation.components["bus"].cents, 3100
        )

    def test_no_suspense_when_every_refund_is_attributed(self):
        self.assertEqual(self.view.suspense(), 0)
        self.assertEqual(self.ledger.open_suspense(), 0)

    def test_total_refund_cash_is_allocated_once(self):
        cash = sum(a.cash_cents for a in self.view.refunds())
        allocated = sum(a.allocated_cents for a in self.view.refunds())
        suspense = sum(a.unallocated_cents for a in self.view.refunds())
        self.assertEqual(cash, allocated + suspense)
        self.assertEqual(allocated, sum(
            line.refund_cents for line in self.view.component_lines()
        ) - sum(
            order.unallocated_refund_cents for order in self.view.orders
        ))


class MaturityTests(Fixture):
    def test_effective_window_extends_to_dispute_resolution(self):
        order = self.view.order_line("O012")
        self.assertEqual(order.effective_window_end, date(2026, 11, 22))
        self.assertGreater(order.effective_window_end, order.order.normal_window_end)
        self.assertTrue(order.mature)

    def test_open_window_batches_are_separated(self):
        immature = [
            order for order in self.view.orders if not order.mature
        ]
        self.assertTrue(immature)
        self.assertEqual(
            {order.order.order_id for order in immature},
            {f"O{number:03d}" for number in range(48, 65)}
            | {f"O{number:03d}" for number in range(65, 73)},
        )
        mature_view = {g.key[0]: g for g in self.view.offer_summary(mature=True)}
        open_view = {g.key[0]: g for g in self.view.offer_summary(mature=False)}
        self.assertEqual(mature_view["P2"].orders + open_view["P2"].orders, 25)

    def test_refund_risk_only_counts_open_windows(self):
        mature_p2 = next(
            g for g in self.view.offer_summary(mature=True) if g.key[0] == "P2"
        )
        self.assertEqual(mature_p2.refund_at_risk_cents, 0)
        open_p3 = next(
            g for g in self.view.offer_summary(mature=False) if g.key[0] == "P3"
        )
        self.assertGreater(open_p3.refund_at_risk_cents, 0)

    def test_future_delivery_is_not_recognised_as_revenue(self):
        view = build_economic_view(self.ds, date(2026, 11, 23))
        p3 = next(g for g in view.offer_summary() if g.key[0] == "P3")
        self.assertEqual(p3.recognized_cents, 0)
        self.assertEqual(p3.deferred_cents, 159600)
        later = build_economic_view(self.ds, date(2026, 12, 17))
        p3_later = next(g for g in later.offer_summary() if g.key[0] == "P3")
        self.assertGreater(p3_later.recognized_cents, 0)


class LedgerTests(Fixture):
    def test_closed_periods_keep_archive_and_are_not_rewritten(self):
        for period in ("2026-09", "2026-10"):
            lines = self.ledger.journal_for(period)
            self.assertTrue(lines)
            self.assertTrue(all(line.closed for line in lines))
            self.assertTrue(all(line.source_kind == "archive" for line in lines))

    def test_later_resolution_posts_to_open_period_with_origin(self):
        lines = [
            line
            for line in self.ledger.lines_for_order("O012")
            if line.source_kind == "dispute_reclass"
        ]
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(line.period, "2026-11")
            self.assertEqual(line.origin_period, "2026-10")
            self.assertFalse(line.closed)

    def test_reclass_releases_suspense_to_zero_net_cash(self):
        lines = [
            line
            for line in self.ledger.lines_for_order("O012")
            if line.source_kind == "dispute_reclass"
        ]
        released = sum(
            line.amount_cents for line in lines if line.account == "suspense_release"
        )
        refunded = sum(
            line.amount_cents for line in lines if line.account == "refund"
        )
        self.assertEqual(released, 6500)
        self.assertEqual(released + refunded, 0)

    def test_archive_reconciliation_matches_source(self):
        matches = self.ledger.archive_reconciliation()
        self.assertEqual(len(matches), len(self.ds.book_entries))
        self.assertTrue(all(match.matched for match in matches))

    def test_closed_month_cash_is_not_double_counted(self):
        september = self.ledger.journal_for("2026-09")
        self.assertFalse(
            any(line.account == "cash_capture" for line in september)
        )

    def test_period_summary_balances(self):
        for summary in self.ledger.period_summary():
            with self.subTest(period=summary.period):
                self.assertEqual(
                    summary.cash_basis_net_cents,
                    summary.cash_capture_cents
                    + summary.issuer_discount_cents
                    + summary.adjustment_cents
                    + summary.refund_cents
                    + summary.suspense_delta_cents,
                )


class ScenarioTests(Fixture):
    def test_stopped_offer_keeps_unavoidable_fixed_and_shared_work(self):
        keep, stop, replace = decision_table(self.ds, self.view, AS_OF)
        stop_terms = {
            (item.term_id, item.month): item.status for item in stop.fixed_detail
        }
        # K3 is non-avoidable; K1 is shared with P3; K2 and K4 can disappear.
        self.assertEqual(stop_terms[("K3", "2026-12")], "committed")
        self.assertEqual(stop_terms[("K1", "2026-12")], "committed")
        self.assertEqual(stop_terms[("K2", "2026-12")], "avoided")

    def test_replacement_keeps_shared_workshop_commitment(self):
        _, _, replace = decision_table(self.ds, self.view, AS_OF)
        replace_terms = {
            (item.term_id, item.month): item.status for item in replace.fixed_detail
        }
        # K1 is shared with P3, so replacing P2 with P3 still needs it.
        self.assertEqual(replace_terms[("K1", "2026-12")], "committed")
        self.assertEqual(replace_terms[("K2", "2026-12")], "avoided")
        # K4 is cancelled from the month after 2026-11-28.
        self.assertNotIn(("K4", "2026-12"), replace_terms)

    def test_scenarios_carry_deferred_and_refund_risk(self):
        _, stop, replace = decision_table(self.ds, self.view, AS_OF)
        for scenario in (stop, replace):
            self.assertGreater(scenario.carried_deferred_cents, 0)
            self.assertGreater(scenario.carried_refund_risk_cents, 0)
        self.assertEqual(stop.carried_deferred_cents, replace.carried_deferred_cents)
        # The observation window closing does not make an unused refundable
        # entitlement free: carried exposure uses the wider eligible figure.
        self.assertGreaterEqual(
            stop.carried_refund_risk_cents,
            stop.unit["P2"].refund_at_risk_cents,
        )
        self.assertEqual(
            stop.carried_refund_risk_cents,
            stop.unit["P2"].refund_eligible_cents,
        )

    def test_keep_still_has_p3_baseline(self):
        keep, stop, _ = decision_table(self.ds, self.view, AS_OF)
        self.assertIn("P3", keep.active_offers)
        self.assertIn("P3", stop.active_offers)
        self.assertIn("P2", keep.active_offers)
        self.assertNotIn("P2", stop.active_offers)

    def test_pending_when_suspense_could_flip_ranking(self):
        keep, stop, _ = decision_table(self.ds, self.view, AS_OF)
        delta = keep.net_monthly_cents - stop.net_monthly_cents
        big = compare_scenarios(
            keep, stop, suspense_claims=[("QX", abs(delta) + 1)]
        )
        self.assertTrue(big.pending)
        self.assertEqual(big.missing_evidence, ("QX",))
        small = compare_scenarios(
            keep, stop, suspense_claims=[("QX", max(abs(delta) - 1, 0))]
        )
        self.assertFalse(small.pending)

    def test_actual_suspense_does_not_flip_current_ranking(self):
        keep, stop, replace = decision_table(self.ds, self.view, AS_OF)
        claims = open_suspense_claims(self.view)
        for pair in ((keep, stop), (keep, replace), (stop, replace)):
            comparison = compare_scenarios(*pair, suspense_claims=claims)
            self.assertFalse(comparison.pending)

    def test_fixed_plan_drops_cancelled_term_after_its_month(self):
        plan = fixed_plan(
            self.ds,
            ("2026-11", "2026-12"),
            active_offers=("P3",),
        )
        k4 = {(item.month, item.status) for item in plan if item.term_id == "K4"}
        # Cancelled on 2026-11-28: only its November month is still payable.
        self.assertEqual(k4, {("2026-11", "committed")})
        plan_later = fixed_plan(
            self.ds,
            ("2026-12",),
            active_offers=("P3",),
        )
        self.assertNotIn("K4", {item.term_id for item in plan_later})

    def test_incurred_fixed_only_counts_months_with_deliveries(self):
        incurred = incurred_fixed(self.ds, self.view)
        self.assertTrue(incurred)
        self.assertEqual(
            {(item.term_id, item.month) for item in incurred},
            {
                ("K1", "2026-09"), ("K1", "2026-10"), ("K1", "2026-11"),
                ("K2", "2026-10"), ("K2", "2026-11"),
                ("K3", "2026-09"), ("K3", "2026-10"), ("K3", "2026-11"),
            },
        )

    def test_fixed_allocation_sums_to_incurred_total(self):
        incurred = incurred_fixed(self.ds, self.view)
        allocated, _ = allocated_incurred_fixed(self.ds, self.view)
        self.assertEqual(sum(allocated.values()), sum(i.cents for i in incurred))


class BillingTests(Fixture):
    def test_next_bill_period_skips_closed_months(self):
        self.assertEqual(next_bill_period(AS_OF, self.ledger), "2026-12")

    def test_bill_run_separates_delivery_from_fixed(self):
        run = build_bill_run(
            self.ds, self.view, self.ledger, next_bill_period(AS_OF, self.ledger)
        )
        delivery = [line for line in run.lines if line.basis == "delivery"]
        fixed = [line for line in run.lines if line.basis == "fixed"]
        self.assertTrue(delivery)
        self.assertTrue(fixed)
        # Fixed lines are one per term still active in the period, never
        # duplicated per offer.
        active = [
            term
            for term in self.ds.supplier_terms
            if term.cancelled_on is None
            or run.period <= period_of(term.cancelled_on)
        ]
        self.assertEqual(len(fixed), len(active))
        self.assertEqual(
            run.total_cents, run.variable_cents + run.fixed_cents
        )
        self.assertEqual(sum(run.by_supplier().values()), run.total_cents)

    def test_bill_run_excludes_cancelled_commitment(self):
        run = build_bill_run(self.ds, self.view, self.ledger, "2026-12")
        self.assertNotIn("K4", {line.reference for line in run.lines})


class TraceTests(Fixture):
    def test_trace_order_shows_supplier_change_and_ledger(self):
        trace = trace_order(self.ds, self.view, self.ledger, "O012")
        lab = next(c for c in trace["components"] if c["component"] == "lab")
        self.assertTrue(lab["supplier_changed"])
        self.assertEqual(lab["sold_supplier"], "B")
        self.assertEqual(lab["actual_supplier"], "D")
        self.assertEqual(lab["voucher_chain"][-1]["coupon_id"], "VR12")
        self.assertTrue(trace["ledger"])

    def test_trace_claim_links_cash_receipts_and_reclass(self):
        trace = trace_claim(self.ds, self.view, self.ledger, "Q12")
        self.assertEqual(trace["refund"]["cash_cents"], 6500)
        self.assertEqual(trace["still_in_suspense_cents"], 0)
        self.assertIn("2026-11", trace["reclassed_in_periods"])
        accounts = {line["account"] for line in trace["ledger_lines"]}
        self.assertIn("suspense_release", accounts)
        self.assertIn("refund", accounts)
        superseded = [
            receipt
            for receipt in trace["refund"]["receipts"]
            if not receipt["final"]
        ]
        self.assertEqual([r["receipt"] for r in superseded], ["D1"])


class AsOfSliceTests(Fixture):
    def test_slice_excludes_later_cash_and_orders(self):
        sliced = self.ds.as_of(date(2026, 11, 23))
        self.assertTrue(all(o.purchase_date <= AS_OF for o in sliced.orders))
        self.assertTrue(all(m.received_date <= AS_OF for m in sliced.money))
        self.assertTrue(all(d.recorded_at <= AS_OF for d in sliced.disputes))
        self.assertNotIn("O072", {o.order_id for o in sliced.orders})

    def test_slice_keeps_books_and_future_obligations(self):
        sliced = self.ds.as_of(date(2026, 11, 23))
        self.assertEqual(
            {b.period for b in sliced.books}, {b.period for b in self.ds.books}
        )
        # A voucher scheduled after the decision date is still a live
        # obligation of an order already sold.
        late = [v for v in sliced.vouchers if v.scheduled and v.scheduled > AS_OF]
        self.assertTrue(late)

    def test_replace_rebuilds_lookup_caches(self):
        base = load_dataset()
        trimmed = base.replace(
            orders=tuple(o for o in base.orders if o.order_id != "O001"),
        )
        with self.assertRaises(Exception):
            trimmed.order("O001")
        self.assertEqual(trimmed.order("O002").order_id, "O002")


class DataContractTests(Fixture):
    def test_refund_resolution_is_stable(self):
        allocations = resolve_refunds(self.ds)
        self.assertEqual(set(allocations), {"Q12", "Q15", "Q46"})

    def test_horizon_is_latest_evidenced_date(self):
        self.assertEqual(self.ds.horizon(), date(2026, 12, 17))

    def test_period_of_matches_iso_month(self):
        self.assertEqual(period_of(date(2026, 11, 23)), "2026-11")


if __name__ == "__main__":
    unittest.main()


class SyntheticSuspenseTests(unittest.TestCase):
    """The shipped data has no unattributable refund, so build one.

    Removing the explicit component from the Q46 refund entry leaves cash with
    no receipt and no channel attribution.  The rules under test are exactly
    the ones that must stop it from being pushed onto a component.
    """

    def setUp(self) -> None:
        from dataclasses import replace

        base = load_dataset()
        money = tuple(
            replace(entry, component="")
            if entry.entry_id == "M-R46"
            else entry
            for entry in base.money
        )
        disputes = tuple(d for d in base.disputes if d.order_id != "O046")
        self.ds = base.replace(money=money, disputes=disputes)
        self.view = build_economic_view(self.ds, AS_OF)
        self.ledger = FinancialLedger(self.ds, self.view, AS_OF)

    def test_cash_without_evidence_stays_in_suspense(self):
        allocation = self.view.refund_for("Q46")
        self.assertEqual(allocation.cash_cents, 3100)
        self.assertEqual(allocation.allocated_cents, 0)
        self.assertEqual(allocation.unallocated_cents, 3100)
        self.assertEqual(allocation.components, {})

    def test_suspense_is_on_its_own_line_not_a_component(self):
        order = self.view.order_line("O046")
        self.assertEqual(order.unallocated_refund_cents, 3100)
        for line in order.components:
            if line.component != UNALLOCATED:
                self.assertEqual(line.refund_cents, 0)
        unallocated = next(
            line for line in order.components if line.component == UNALLOCATED
        )
        self.assertEqual(unallocated.refund_cents, 3100)
        self.assertEqual(unallocated.state, "unallocated")

    def test_suspense_is_not_recognised_or_deferred_revenue(self):
        order = self.view.order_line("O046")
        component_net = sum(
            line.net_cents for line in order.components
            if line.component != UNALLOCATED
        )
        self.assertEqual(component_net - 3100, order.net_cents)
        self.assertEqual(
            order.deferred_cents,
            sum(line.deferred_cents for line in order.components
                if line.component != UNALLOCATED),
        )

    def test_ledger_holds_cash_in_open_period_and_reports_it(self):
        item = next(i for i in self.ledger.suspense_report() if i.claim == "Q46")
        self.assertEqual(item.amount_cents, 3100)
        self.assertEqual(item.status, "open")
        self.assertIn("尚未确认归属", item.reason)
        suspense_lines = [
            line
            for line in self.ledger.lines_for_order("O046")
            if line.account == "refund_suspense"
        ]
        self.assertEqual(sum(line.amount_cents for line in suspense_lines), -3100)

    def test_open_suspense_exposes_the_unclaimed_cash(self):
        self.assertEqual(self.ledger.open_suspense(), 3100)
        payload = [
            (claim, cents) for claim, cents in open_suspense_claims(self.view)
        ]
        self.assertEqual(payload, [("Q46", 3100)])

    def test_pending_flagged_when_gap_is_below_suspense(self):
        keep, stop, _ = decision_table(self.ds, self.view, AS_OF)
        # An artificially tiny gap demonstrates the flip guard.
        comparison = compare_scenarios(
            keep, stop, suspense_claims=[("Q46", 3100)]
        )
        if abs(keep.net_monthly_cents - stop.net_monthly_cents) <= 3100:
            self.assertTrue(comparison.pending)
        else:
            self.assertFalse(comparison.pending)
            self.assertIn("低于", comparison.note)
