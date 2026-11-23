"""Next-period supplier bill run.

The operating group's recurring job is to turn the next month's expected
service into payable bills.  This module does that without inventing demand:
it bills only what is *already scheduled* on live vouchers (the performance
obligations the alliance has taken on), plus the monthly commitments that the
contract says are still payable.

Output is per supplier/component so shared fixed commitments (a workshop used
by two offers) are visible rather than duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Tuple

from .allocation import EconomicView, resolve_leaf_vouchers
from .financial import FinancialLedger, next_period, period_of
from .model import Dataset
from .scenario import term_active_in_month


def next_bill_period(as_of: date, ledger: FinancialLedger) -> str:
    """First open month strictly after the decision month."""

    candidate = next_period(period_of(as_of))
    for _ in range(240):
        if not ledger.is_closed(candidate):
            return candidate
        candidate = next_period(candidate)
    raise ValueError("no open period available for billing")


@dataclass(frozen=True)
class BillLine:
    supplier: str
    component: str
    basis: str
    units: int
    unit_cents: int
    amount_cents: int
    reference: str
    offers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BillRun:
    period: str
    lines: Tuple[BillLine, ...]
    cut_off: date
    notes: Tuple[str, ...] = ()

    @property
    def variable_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines if line.basis == "delivery")

    @property
    def fixed_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines if line.basis == "fixed")

    @property
    def total_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines)

    def by_supplier(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for line in self.lines:
            out[line.supplier] = out.get(line.supplier, 0) + line.amount_cents
        return dict(sorted(out.items()))


def build_bill_run(
    ds: Dataset, view: EconomicView, ledger: FinancialLedger, period: str
) -> BillRun:
    leaves = resolve_leaf_vouchers(ds)
    scheduled: Dict[Tuple[str, str], Dict[str, object]] = {}
    for (order_id, component), voucher in leaves.items():
        if voucher.scheduled is None:
            continue
        if period_of(voucher.scheduled) != period:
            continue
        order = ds.order(order_id)
        key = (voucher.supplier, component)
        bucket = scheduled.setdefault(
            key, {"units": 0, "offers": set(), "refs": []}
        )
        bucket["units"] += 1
        bucket["offers"].add(order.offer)
        bucket["refs"].append(voucher.coupon_id)

    lines: List[BillLine] = []
    for (supplier, component), bucket in sorted(scheduled.items()):
        units = int(bucket["units"])
        rate = ds.rate_for(
            sorted(bucket["offers"])[0], component, supplier
        )
        lines.append(
            BillLine(
                supplier=supplier,
                component=component,
                basis="delivery",
                units=units,
                unit_cents=rate,
                amount_cents=units * rate,
                reference=",".join(sorted(bucket["refs"])),
                offers=tuple(sorted(bucket["offers"])),
            )
        )

    # "Still on sale" means the offer still has a live, undelivered
    # entitlement.  A fully delivered offer must not keep a shared fixed
    # commitment alive in the bill run.
    active_offers = {
        ds.order(order_id).offer
        for (order_id, _), voucher in leaves.items()
        if voucher.state == "unused"
    }
    unused_leaves = {
        (order_id, component): voucher
        for (order_id, component), voucher in leaves.items()
        if voucher.state == "unused"
    }
    for term in ds.supplier_terms:
        if not term_active_in_month(term, period):
            continue
        shared = tuple(
            sorted(
                {
                    ds.order(order_id).offer
                    for (order_id, component), voucher in unused_leaves.items()
                    if component == term.component
                    and voucher.supplier == term.supplier
                }
            )
        )
        lines.append(
            BillLine(
                supplier=term.supplier,
                component=term.component,
                basis="fixed",
                units=1,
                unit_cents=term.fixed_cents,
                amount_cents=term.fixed_cents,
                reference=term.term_id,
                offers=shared,
            )
        )

    notes = [
        "delivery lines are scheduled live vouchers only, not a demand forecast",
        "fixed lines are monthly commitments still active in the period",
    ]
    for term in ds.supplier_terms:
        if term.cancelled_on and period > period_of(term.cancelled_on):
            notes.append(
                f"{term.term_id} cancelled {term.cancelled_on.isoformat()}: "
                "no fixed charge after that month"
            )
    return BillRun(period=period, lines=tuple(lines), cut_off=view.as_of, notes=tuple(notes))


def bill_run_dict(run: BillRun) -> Dict[str, object]:
    return {
        "period": run.period,
        "cut_off": run.cut_off.isoformat(),
        "variable_cents": run.variable_cents,
        "fixed_cents": run.fixed_cents,
        "total_cents": run.total_cents,
        "by_supplier": run.by_supplier(),
        "lines": [
            {
                "supplier": line.supplier,
                "component": line.component,
                "basis": line.basis,
                "units": line.units,
                "unit_cents": line.unit_cents,
                "amount_cents": line.amount_cents,
                "reference": line.reference,
                "offers": list(line.offers),
            }
            for line in run.lines
        ],
        "notes": list(run.notes),
    }
