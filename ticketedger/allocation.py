"""Ticket-level allocation and revenue recognition.

The economic view answers: for each component of each family ticket, how much
of the money collected belongs to it, how much has been earned by actually
delivering that component, what is still owed, what has been refunded, and who
delivered it.

Allocation rules
----------------
* The distributable pool is the cash captured for the order minus any
  ``whole_coupon`` discount the issuer funds.  A platform-funded discount is a
  memo: it does not reduce what the alliance keeps, so it is not netted again.
* The pool is split by the sold offer version's published weights.  Integer
  cents are assigned by largest remainder so the components sum to the pool.
* Price adjustments are charged to the named component.
* Refund cash is assigned only through final dispute receipts or an explicit
  channel component; anything else stays in suspense on a dedicated
  ``unallocated`` line and is never pushed onto a component.
* Only a live, redeemed voucher earns service revenue.  A live but unused
  voucher is a performance obligation, not revenue.  A void voucher earns
  nothing; its successor carries the right.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .model import Dataset, Order, Voucher
from .refunds import RefundAllocation, resolve_refunds

UNALLOCATED = "unallocated"
UNALLOCATED_REFUND_POLICY = "none"


@dataclass(frozen=True)
class ComponentLine:
    order_id: str
    offer: str
    channel: str
    household: str
    purchase_date: date
    purchase_period: str
    component: str
    weight: int
    gross_cents: int
    issuer_discount_cents: int
    issuer_share_cents: int
    adjustment_cents: int
    refund_cents: int
    net_cents: int
    state: str
    refundable: str
    sold_supplier: str
    supplier: str
    voucher_id: str
    voucher_chain: Tuple[str, ...]
    scheduled: Optional[date]
    redeemed: Optional[date]
    service_period: Optional[str]
    rate_cents: int
    refund_receipts: Tuple[str, ...]
    refund_basis: str
    effective_window_end: date
    mature: bool
    as_of: date
    delivered: bool

    @property
    def supplier_changed(self) -> bool:
        return self.sold_supplier != self.supplier

    @property
    def redeemed_state(self) -> bool:
        """Service was actually delivered no later than the decision date."""

        return self.state == "redeemed" and self.delivered

    @property
    def future_delivery(self) -> bool:
        return self.state == "redeemed" and not self.delivered

    @property
    def recognized_cents(self) -> int:
        return self.net_cents if self.redeemed_state else 0

    @property
    def deferred_cents(self) -> int:
        """Money not yet earned: unused or scheduled after the decision date."""

        if self.component == UNALLOCATED:
            return 0
        return self.net_cents if not self.redeemed_state else 0

    @property
    def variable_cost_cents(self) -> int:
        return self.rate_cents if self.redeemed_state else 0

    @property
    def contribution_cents(self) -> int:
        return self.recognized_cents - self.variable_cost_cents

    @property
    def expected_contribution_cents(self) -> int:
        """Contribution if the still-live entitlement is delivered.

        A component that has been refunded to zero is not expected to be
        delivered, so its per-delivery cost is not counted either.  This is the
        forward-looking counterpart to ``contribution_cents`` and is what the
        stop/replace scenarios must use, otherwise an offer whose service is
        all scheduled for next month looks as if it were worthless.
        """

        if self.component == UNALLOCATED or self.state == "void":
            return 0
        if self.net_cents <= 0:
            return 0
        return self.net_cents - self.rate_cents

    @property
    def refund_at_risk_cents(self) -> int:
        """Cash that could still be refunded without breaking a delivered right."""

        if self.net_cents <= 0:
            return 0
        if self.mature:
            return 0
        if not self.redeemed_state and self.refundable in ("unused", "open"):
            return self.net_cents
        if self.redeemed_state and self.refundable == "open":
            return self.net_cents
        return 0

    @property
    def refund_eligible_cents(self) -> int:
        """Unearned or open-policy cash, regardless of how mature the order is.

        A mature order is simply outside the *observation window*; it does not
        convert an unused, refundable entitlement into guaranteed revenue.  The
        forward scenarios use this wider figure for what still has to be
        honoured or could still be returned.
        """

        if self.component == UNALLOCATED or self.net_cents <= 0:
            return 0
        if not self.redeemed_state and self.refundable in ("unused", "open"):
            return self.net_cents
        if self.redeemed_state and self.refundable == "open":
            return self.net_cents
        return 0


@dataclass(frozen=True)
class OrderLine:
    order: Order
    offer: str
    channel: str
    purchase_period: str
    capture_cents: int
    issuer_discount_cents: int
    platform_discount_cents: int
    distributor_pool_cents: int
    components: Tuple[ComponentLine, ...]
    refunds: Tuple[RefundAllocation, ...]
    unallocated_refund_cents: int
    effective_window_end: date
    mature: bool

    @property
    def net_cents(self) -> int:
        return sum(line.net_cents for line in self.components)

    @property
    def recognized_cents(self) -> int:
        return sum(line.recognized_cents for line in self.components)

    @property
    def deferred_cents(self) -> int:
        return sum(line.deferred_cents for line in self.components)

    @property
    def variable_cost_cents(self) -> int:
        return sum(line.variable_cost_cents for line in self.components)

    @property
    def contribution_cents(self) -> int:
        return self.recognized_cents - self.variable_cost_cents

    @property
    def expected_contribution_cents(self) -> int:
        """Contribution if the still-live entitlement is delivered.

        A component that has been refunded to zero is not expected to be
        delivered, so its per-delivery cost is not counted either.  This is the
        forward-looking counterpart to ``contribution_cents`` and is what the
        stop/replace scenarios must use, otherwise an offer whose service is
        all scheduled for next month looks as if it were worthless.
        """

        if self.component == UNALLOCATED or self.state == "void":
            return 0
        if self.net_cents <= 0:
            return 0
        return self.net_cents - self.rate_cents

    @property
    def refund_at_risk_cents(self) -> int:
        return sum(line.refund_at_risk_cents for line in self.components)

    @property
    def refund_eligible_cents(self) -> int:
        return sum(line.refund_eligible_cents for line in self.components)


def _period(day: date) -> str:
    return day.strftime("%Y-%m")


def allocate_weighted(total: int, weights: Sequence[Tuple[str, int]]) -> Dict[str, int]:
    """Split ``total`` cents by integer weights with largest-remainder rounding."""

    weight_total = sum(weight for _, weight in weights)
    if weight_total <= 0:
        raise ValueError("weights must sum to a positive value")
    raw = [(key, total * weight / weight_total) for key, weight in weights]
    assigned = {key: int(value // 1) for key, value in raw}
    remainder = total - sum(assigned.values())
    ranked = sorted(raw, key=lambda item: (-(item[1] - int(item[1] // 1)), item[0]))
    for key, _ in ranked[:remainder]:
        assigned[key] += 1
    return assigned


def resolve_leaf_vouchers(ds: Dataset) -> Dict[Tuple[str, str], Voucher]:
    """Return the live voucher for each order component.

    A voucher superseded by a successor, or explicitly void, is history.  The
    successor inherits the entitlement and may change supplier or guest.
    """

    superseded = {v.previous for v in ds.vouchers if v.previous}
    leaves: Dict[Tuple[str, str], Voucher] = {}
    for voucher in ds.vouchers:
        if voucher.coupon_id in superseded or voucher.state == "void":
            continue
        key = (voucher.order_id, voucher.component)
        if key in leaves:
            raise ValueError(f"multiple live vouchers for {key}")
        leaves[key] = voucher
    return leaves


def voucher_chain(ds: Dataset, leaf: Voucher) -> Tuple[str, ...]:
    chain = [leaf.coupon_id]
    previous = leaf.previous
    seen = {leaf.coupon_id}
    while previous:
        if previous in seen:
            raise ValueError(f"voucher cycle at {previous}")
        seen.add(previous)
        chain.append(previous)
        previous = ds.voucher(previous).previous
    return tuple(reversed(chain))


def effective_windows(
    ds: Dataset, allocations: Mapping[str, RefundAllocation]
) -> Dict[str, date]:
    """Refund-observation end date per order.

    The normal 14-day window is extended by the latest resolution date of any
    dispute attached to the order.
    """

    windows = {order.order_id: order.normal_window_end for order in ds.orders}
    for allocation in allocations.values():
        current = windows[allocation.order_id]
        if allocation.final_resolution and allocation.final_resolution > current:
            windows[allocation.order_id] = allocation.final_resolution
    return windows


def build_economic_view(ds: Dataset, as_of: date) -> "EconomicView":
    allocations = resolve_refunds(ds)
    refunds_by_order: Dict[str, List[RefundAllocation]] = {}
    for allocation in allocations.values():
        refunds_by_order.setdefault(allocation.order_id, []).append(allocation)
    windows = effective_windows(ds, allocations)
    leaves = resolve_leaf_vouchers(ds)

    issuer_discount: Dict[str, int] = {}
    platform_discount: Dict[str, int] = {}
    captures: Dict[str, int] = {}
    adjustments: Dict[Tuple[str, str], int] = {}
    for entry in ds.money:
        if entry.kind == "capture":
            captures[entry.order_id] = captures.get(entry.order_id, 0) + entry.amount_cents
        elif entry.kind == "whole_coupon":
            target = issuer_discount if entry.funder == "issuer" else platform_discount
            target[entry.order_id] = target.get(entry.order_id, 0) + entry.amount_cents
        elif entry.kind == "price_adjustment":
            key = (entry.order_id, entry.component)
            adjustments[key] = adjustments.get(key, 0) + entry.amount_cents

    order_lines: List[OrderLine] = []
    for order in ds.orders:
        capture = captures.get(order.order_id, 0)
        issuer = issuer_discount.get(order.order_id, 0)
        platform = platform_discount.get(order.order_id, 0)
        pool = capture - issuer
        if pool < 0:
            raise ValueError(f"order {order.order_id}: issuer discount exceeds capture")
        components = ds.offer_components(order.offer)
        weights = [(row.component, row.weight) for row in components]
        split = allocate_weighted(pool, weights)
        issuer_split = allocate_weighted(issuer, weights)
        order_refunds = sorted(
            refunds_by_order.get(order.order_id, ()), key=lambda a: a.claim
        )
        refund_by_component: Dict[str, int] = {}
        receipt_by_component: Dict[str, List[str]] = {}
        basis_by_component: Dict[str, str] = {}
        for allocation in order_refunds:
            for component, item in allocation.components.items():
                refund_by_component[component] = (
                    refund_by_component.get(component, 0) + item.cents
                )
                receipt_by_component.setdefault(component, []).extend(item.receipt_ids)
                basis_by_component[component] = item.basis
        unallocated = sum(a.unallocated_cents for a in order_refunds)

        lines: List[ComponentLine] = []
        for row in components:
            leaf = leaves[(order.order_id, row.component)]
            gross = split[row.component]
            adjustment = adjustments.get((order.order_id, row.component), 0)
            refund = refund_by_component.get(row.component, 0)
            lines.append(
                ComponentLine(
                    order_id=order.order_id,
                    offer=order.offer,
                    channel=order.channel,
                    household=order.household,
                    purchase_date=order.purchase_date,
                    purchase_period=_period(order.purchase_date),
                    component=row.component,
                    weight=row.weight,
                    gross_cents=gross,
                    issuer_discount_cents=issuer,
                    issuer_share_cents=issuer_split[row.component],
                    adjustment_cents=adjustment,
                    refund_cents=refund,
                    net_cents=gross - adjustment - refund,
                    state=leaf.state,
                    refundable=row.refundable,
                    sold_supplier=row.supplier,
                    supplier=leaf.supplier,
                    voucher_id=leaf.coupon_id,
                    voucher_chain=voucher_chain(ds, leaf),
                    scheduled=leaf.scheduled,
                    redeemed=leaf.redeemed,
                    service_period=_period(leaf.redeemed) if leaf.redeemed else None,
                    rate_cents=ds.rate_for(order.offer, row.component, leaf.supplier),
                    refund_receipts=tuple(receipt_by_component.get(row.component, ())),
                    refund_basis=basis_by_component.get(row.component, "none"),
                    effective_window_end=windows[order.order_id],
                    mature=as_of > windows[order.order_id],
                    as_of=as_of,
                    delivered=(
                        leaf.redeemed is not None and leaf.redeemed <= as_of
                    ),
                )
            )

        if unallocated:
            lines.append(
                ComponentLine(
                    order_id=order.order_id,
                    offer=order.offer,
                    channel=order.channel,
                    household=order.household,
                    purchase_date=order.purchase_date,
                    purchase_period=_period(order.purchase_date),
                    component=UNALLOCATED,
                    weight=0,
                    gross_cents=0,
                    issuer_discount_cents=0,
                    issuer_share_cents=0,
                    adjustment_cents=0,
                    refund_cents=unallocated,
                    net_cents=-unallocated,
                    state="unallocated",
                    refundable=UNALLOCATED_REFUND_POLICY,
                    sold_supplier="",
                    supplier="",
                    voucher_id="",
                    voucher_chain=(),
                    scheduled=None,
                    redeemed=None,
                    service_period=None,
                    rate_cents=0,
                    refund_receipts=(),
                    refund_basis="unresolved",
                    effective_window_end=windows[order.order_id],
                    mature=as_of > windows[order.order_id],
                    as_of=as_of,
                    delivered=False,
                )
            )

        order_lines.append(
            OrderLine(
                order=order,
                offer=order.offer,
                channel=order.channel,
                purchase_period=_period(order.purchase_date),
                capture_cents=capture,
                issuer_discount_cents=issuer,
                platform_discount_cents=platform,
                distributor_pool_cents=pool,
                components=tuple(lines),
                refunds=tuple(order_refunds),
                unallocated_refund_cents=unallocated,
                effective_window_end=windows[order.order_id],
                mature=as_of > windows[order.order_id],
            )
        )

    return EconomicView(ds=ds, as_of=as_of, lineage_tuples=tuple(order_lines))


@dataclass(frozen=True)
class GroupMetrics:
    key: Tuple[str, ...]
    orders: int
    capture_cents: int
    issuer_discount_cents: int
    adjustment_cents: int
    refund_cents: int
    unallocated_refund_cents: int
    net_cents: int
    recognized_cents: int
    deferred_cents: int
    variable_cost_cents: int
    contribution_cents: int
    refund_at_risk_cents: int
    realized_components: int
    unfulfilled_components: int

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "orders": self.orders,
            "capture_cents": self.capture_cents,
            "issuer_discount_cents": self.issuer_discount_cents,
            "adjustment_cents": self.adjustment_cents,
            "refund_cents": self.refund_cents,
            "unallocated_refund_cents": self.unallocated_refund_cents,
            "net_cents": self.net_cents,
            "recognized_cents": self.recognized_cents,
            "deferred_cents": self.deferred_cents,
            "variable_cost_cents": self.variable_cost_cents,
            "contribution_cents": self.contribution_cents,
            "refund_at_risk_cents": self.refund_at_risk_cents,
            "realized_components": self.realized_components,
            "unfulfilled_components": self.unfulfilled_components,
        }


class EconomicView:
    """Operating view: money follows the sold ticket and the delivered service."""

    def __init__(
        self,
        ds: Dataset,
        as_of: date,
        lineage_tuples: Tuple[OrderLine, ...],
    ) -> None:
        self.ds = ds
        self.as_of = as_of
        self.orders: Tuple[OrderLine, ...] = lineage_tuples

    # -- accessors -------------------------------------------------------
    def order_line(self, order_id: str) -> OrderLine:
        for line in self.orders:
            if line.order.order_id == order_id:
                return line
        raise KeyError(order_id)

    def component_lines(self) -> Tuple[ComponentLine, ...]:
        return tuple(
            component for order in self.orders for component in order.components
        )

    def component_line(self, order_id: str, component: str) -> ComponentLine:
        for line in self.component_lines():
            if line.order_id == order_id and line.component == component:
                return line
        raise KeyError((order_id, component))

    def refunds(self) -> Tuple[RefundAllocation, ...]:
        return tuple(allocation for order in self.orders for allocation in order.refunds)

    def refund_for(self, claim: str) -> RefundAllocation:
        for allocation in self.refunds():
            if allocation.claim == claim:
                return allocation
        raise KeyError(claim)

    # -- grouping --------------------------------------------------------
    def group(
        self,
        dimensions: Sequence[str],
        *,
        mature: Optional[bool] = None,
        include_unserved: bool = True,
    ) -> Tuple[GroupMetrics, ...]:
        """Aggregate by any of offer/purchase_period/service_period/channel.

        Component rows without a service month are grouped under the literal
        ``unserved`` when ``include_unserved`` is true, so deferred revenue and
        refund exposure never disappear from a "接待月份" comparison.  The same
        applies to the ``unallocated`` refund line.
        """

        allowed = {"offer", "purchase_period", "service_period", "channel"}
        unknown = set(dimensions) - allowed
        if unknown:
            raise ValueError(f"unknown dimensions {sorted(unknown)}")
        if not dimensions:
            raise ValueError("at least one dimension is required")

        buckets: Dict[Tuple[str, ...], Dict[str, object]] = {}
        for order in self.orders:
            if mature is not None and order.mature != mature:
                continue
            for line in order.components:
                if not include_unserved and line.service_period is None:
                    continue
                key = tuple(self._dimension(line, name) for name in dimensions)
                bucket = buckets.setdefault(
                    key,
                    {
                        "order_ids": set(),
                        "capture": 0,
                        "issuer": 0,
                        "adjustment": 0,
                        "refund": 0,
                        "unallocated": 0,
                        "net": 0,
                        "recognized": 0,
                        "deferred": 0,
                        "variable": 0,
                        "risk": 0,
                        "realized": 0,
                        "unfulfilled": 0,
                    },
                )
                bucket["order_ids"].add(line.order_id)
                bucket["capture"] += line.gross_cents + line.issuer_share_cents
                bucket["issuer"] += line.issuer_share_cents
                bucket["adjustment"] += line.adjustment_cents
                bucket["refund"] += line.refund_cents
                if line.component == UNALLOCATED:
                    bucket["unallocated"] += line.refund_cents
                bucket["net"] += line.net_cents
                bucket["recognized"] += line.recognized_cents
                bucket["deferred"] += line.deferred_cents
                bucket["variable"] += line.variable_cost_cents
                bucket["risk"] += line.refund_at_risk_cents
                if line.redeemed_state:
                    bucket["realized"] += 1
                elif line.state in ("unused", "redeemed"):
                    bucket["unfulfilled"] += 1

        results: List[GroupMetrics] = []
        for key, bucket in buckets.items():
            results.append(
                GroupMetrics(
                    key=key,
                    orders=len(bucket["order_ids"]),
                    capture_cents=bucket["capture"],
                    issuer_discount_cents=bucket["issuer"],
                    adjustment_cents=bucket["adjustment"],
                    refund_cents=bucket["refund"],
                    unallocated_refund_cents=bucket["unallocated"],
                    net_cents=bucket["net"],
                    recognized_cents=bucket["recognized"],
                    deferred_cents=bucket["deferred"],
                    variable_cost_cents=bucket["variable"],
                    contribution_cents=bucket["recognized"] - bucket["variable"],
                    refund_at_risk_cents=bucket["risk"],
                    realized_components=bucket["realized"],
                    unfulfilled_components=bucket["unfulfilled"],
                )
            )
        results.sort(key=lambda item: item.key)
        return tuple(results)

    def _dimension(self, line: ComponentLine, name: str) -> str:
        if name == "offer":
            return line.offer
        if name == "purchase_period":
            return line.purchase_period
        if name == "service_period":
            return line.service_period or "unserved"
        if name == "channel":
            return line.channel
        raise ValueError(name)

    # -- convenience -----------------------------------------------------
    def offer_summary(self, mature: Optional[bool] = None) -> Tuple[GroupMetrics, ...]:
        return self.group(("offer",), mature=mature)

    def batch_summary(self, mature: Optional[bool] = None) -> Tuple[GroupMetrics, ...]:
        return self.group(("purchase_period", "offer", "channel"), mature=mature)

    def month_summary(self, mature: Optional[bool] = None) -> Tuple[GroupMetrics, ...]:
        return self.group(("service_period", "offer"), mature=mature)

    def channel_summary(self, mature: Optional[bool] = None) -> Tuple[GroupMetrics, ...]:
        return self.group(("channel", "offer"), mature=mature)

    def outstanding(self, offer: Optional[str] = None) -> Tuple[ComponentLine, ...]:
        return tuple(
            line
            for line in self.component_lines()
            if line.state in ("unused", "redeemed")
            and not line.redeemed_state
            and (offer is None or line.offer == offer)
        )

    def refund_risk(self, offer: Optional[str] = None) -> int:
        return sum(
            line.refund_at_risk_cents
            for line in self.component_lines()
            if offer is None or line.offer == offer
        )

    def suspense(self) -> int:
        return sum(order.unallocated_refund_cents for order in self.orders)
