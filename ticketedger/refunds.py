"""Refund-claim resolution.

A refund cash entry may cover several components.  The channel often leaves
``component`` blank, in which case the dispute receipts are the only evidence
that can attribute the money.  Two rules matter for correctness:

* a receipt that another receipt ``replacement_of`` is a superseded version
  of the same claim and is never added on top of its replacement;
* whatever the receipts do not identify stays in suspense.  It is not pushed
  onto the component that happens to be nearest to the refund.

The module produces one allocation per claim and keeps the receipt chain so a
ticket trace can show exactly which document supported each component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .model import DataError, Dataset, Dispute, Money


@dataclass(frozen=True)
class ReceiptTrace:
    receipt: str
    component: str
    resolved_cents: int
    result: str
    replacement_of: str
    recorded_at: date
    latest_resolution: date
    superseded_by: Tuple[str, ...] = ()

    @property
    def is_final(self) -> bool:
        return not self.superseded_by


@dataclass
class ComponentAllocation:
    component: str
    cents: int
    receipt_ids: Tuple[str, ...]
    # ``receipt`` when a final dispute receipt names the component,
    # ``channel`` when the refund entry itself names it, else ``unresolved``.
    basis: str

    @property
    def confirmed(self) -> bool:
        return self.basis in ("receipt", "channel")


@dataclass
class RefundAllocation:
    claim: str
    order_id: str
    refund_entry_ids: Tuple[str, ...]
    cash_cents: int
    components: Dict[str, ComponentAllocation] = field(default_factory=dict)
    unallocated_cents: int = 0
    receipts: Tuple[ReceiptTrace, ...] = ()
    final_resolution: Optional[date] = None
    refund_period: Optional[str] = None
    notes: Tuple[str, ...] = ()

    @property
    def allocated_cents(self) -> int:
        return sum(item.cents for item in self.components.values())

    @property
    def unallocated(self) -> bool:
        return self.unallocated_cents > 0

    def component_cents(self, component: str) -> int:
        item = self.components.get(component)
        return item.cents if item else 0


def _period(day: date) -> str:
    return day.strftime("%Y-%m")


def build_receipt_traces(disputes: Sequence[Dispute]) -> Tuple[ReceiptTrace, ...]:
    """Attach supersession links, keeping only final receipts as evidence."""

    superseded_by: Dict[str, List[str]] = {}
    for dispute in disputes:
        if dispute.replacement_of:
            superseded_by.setdefault(dispute.replacement_of, []).append(dispute.receipt)
    return tuple(
        ReceiptTrace(
            receipt=d.receipt,
            component=d.component,
            resolved_cents=d.resolved_cents,
            result=d.result,
            replacement_of=d.replacement_of,
            recorded_at=d.recorded_at,
            latest_resolution=d.latest_resolution,
            superseded_by=tuple(sorted(superseded_by.get(d.receipt, ()))),
        )
        for d in disputes
    )


def resolve_refunds(ds: Dataset) -> Dict[str, RefundAllocation]:
    """Resolve every refund claim exactly once.

    Returns a mapping keyed by ``money.claim``.  Refund entries without a
    claim cannot be attributed and raise, because silently pooling them would
    make the ticket trace ambiguous.
    """

    traces = build_receipt_traces(ds.disputes)
    by_claim: Dict[str, List[ReceiptTrace]] = {}
    for trace in traces:
        by_claim.setdefault(_claim_of(ds, trace.receipt), []).append(trace)

    refunds_by_claim: Dict[str, List[Money]] = {}
    for entry in ds.money:
        if entry.kind != "refund":
            continue
        if not entry.claim:
            raise DataError(
                f"refund {entry.entry_id} has no claim and cannot be traced"
            )
        refunds_by_claim.setdefault(entry.claim, []).append(entry)

    allocations: Dict[str, RefundAllocation] = {}
    for claim, entries in refunds_by_claim.items():
        claim_traces = tuple(
            sorted(by_claim.get(claim, ()), key=lambda t: (t.recorded_at, t.receipt))
        )
        cash = sum(entry.amount_cents for entry in entries)
        order_id = entries[0].order_id
        if any(entry.order_id != order_id for entry in entries):
            raise DataError(f"refund claim {claim} spans multiple orders")

        finals = [t for t in claim_traces if t.is_final]
        resolution_dates = [t.latest_resolution for t in finals] or [
            e.event_date for e in entries
        ]
        final_resolution = max(resolution_dates)
        allocation = RefundAllocation(
            claim=claim,
            order_id=order_id,
            refund_entry_ids=tuple(e.entry_id for e in entries),
            cash_cents=cash,
            receipts=claim_traces,
            final_resolution=final_resolution,
            refund_period=_period(min(e.received_date for e in entries)),
        )

        final_total = sum(t.resolved_cents for t in finals)
        if final_total > cash:
            raise DataError(
                f"claim {claim}: final receipts {final_total} exceed refund cash {cash}"
            )

        if finals:
            grouped: Dict[str, List[ReceiptTrace]] = {}
            for trace in finals:
                grouped.setdefault(trace.component, []).append(trace)
            for component, group in grouped.items():
                cents = sum(t.resolved_cents for t in group)
                allocation.components[component] = ComponentAllocation(
                    component=component,
                    cents=cents,
                    receipt_ids=tuple(t.receipt for t in group),
                    basis="receipt",
                )
            if final_total < cash:
                allocation.unallocated_cents = cash - final_total
                allocation.notes += (
                    f"回执只确认 {final_total}，其余 {cash - final_total} 留在待认领",
                )
        else:
            # No receipt at all: only an explicit component on the cash entry
            # can attribute the money.
            explicit = {e.component for e in entries if e.component}
            if len(explicit) == 1:
                component = explicit.pop()
                allocation.components[component] = ComponentAllocation(
                    component=component,
                    cents=cash,
                    receipt_ids=(),
                    basis="channel",
                )
            elif len(explicit) > 1:
                raise DataError(f"claim {claim}: refund entries name several components")
            else:
                allocation.unallocated_cents = cash
                allocation.notes += (
                    "没有争议回执，退款凭证也未写子权益；"
                    "收款渠道尚未确认归属",
                )

        for trace in claim_traces:
            if trace.superseded_by:
                allocation.notes += (
                    f"{trace.receipt}（{trace.resolved_cents}）已被 "
                    f"{', '.join(trace.superseded_by)} 替换，只按最终版本计一次",
                )
        allocations[claim] = allocation

    return allocations


def _claim_of(ds: Dataset, receipt: str) -> str:
    for dispute in ds.disputes:
        if dispute.receipt == receipt:
            return dispute.claim
    raise DataError(f"unknown receipt {receipt!r}")
