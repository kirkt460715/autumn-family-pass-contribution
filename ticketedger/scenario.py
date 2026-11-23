"""Forward comparison of keeping, stopping and replacing a combo offer.

Three traps drive the design:

1. **Only avoidable fixed cost disappears.**  A monthly supplier commitment is
   removed from a scenario only when its contract says it is avoidable *and*
   no remaining active offer still uses that supplier on that component.
   Shared workshops stay in the comparison.
2. **Old tickets are still honoured.**  Stopping an offer cancels future sales,
   not the performance obligations already sold.  Unfulfilled service and
   refund-eligible cash are carried by every scenario that stops the offer; an
   offer kept on sale still carries its own obligations inside its run-rate.
3. **An unclaimed amount can flip the answer.**  When refund cash is still in
   suspense and its size is at least the gap between two scenarios, the
   comparison is reported as *pending* and the missing evidence is named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from .allocation import UNALLOCATED, EconomicView, allocate_weighted
from .model import Dataset, OfferComponent, SupplierTerm


def _period(day: date) -> str:
    return day.strftime("%Y-%m")


def month_months(start: str, count: int) -> Tuple[str, ...]:
    year, month = (int(part) for part in start.split("-"))
    out = []
    for _ in range(count):
        out.append(f"{year}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(out)


def forward_months(as_of: date, count: int = 3) -> Tuple[str, ...]:
    """The first ``count`` months that are not fully in the past."""

    return month_months(_period(as_of), count)


def term_active_in_month(term: SupplierTerm, month: str) -> bool:
    if term.cancelled_on is None:
        return True
    return month <= _period(term.cancelled_on)


def term_due_in_month(term: SupplierTerm, month: str) -> bool:
    """Whether a fixed commitment is billed in ``month``.

    The supplied terms are monthly commitments, so every active month is due.
    A future source that bills quarterly or annually can mark it here without
    changing the comparison logic.
    """

    return True


def _offer_supplier_pairs(
    rows: Sequence[OfferComponent],
) -> set[Tuple[str, str, str]]:
    return {(row.supplier, row.component, row.offer) for row in rows}


@dataclass(frozen=True)
class FixedTermMonth:
    month: str
    term_id: str
    supplier: str
    component: str
    cents: int
    avoidable: bool
    status: str
    shared_with: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "month": self.month,
            "term_id": self.term_id,
            "supplier": self.supplier,
            "component": self.component,
            "cents": self.cents,
            "avoidable": self.avoidable,
            "status": self.status,
            "shared_with": list(self.shared_with),
        }


@dataclass(frozen=True)
class UnitEconomics:
    offer: str
    orders: int
    purchase_months: int
    orders_per_month: float
    capture_cents: int
    net_cents: int
    recognized_cents: int
    deferred_cents: int
    variable_cost_cents: int
    contribution_cents: int
    expected_contribution_cents: int
    contribution_per_order: float
    refund_at_risk_cents: int
    refund_eligible_cents: int
    realized_components: int
    unfulfilled_components: int
    mature_orders: int
    open_window_orders: int
    basis: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "offer": self.offer,
            "orders": self.orders,
            "purchase_months": self.purchase_months,
            "orders_per_month": self.orders_per_month,
            "capture_cents": self.capture_cents,
            "net_cents": self.net_cents,
            "recognized_cents": self.recognized_cents,
            "deferred_cents": self.deferred_cents,
            "variable_cost_cents": self.variable_cost_cents,
            "contribution_cents": self.contribution_cents,
            "expected_contribution_cents": self.expected_contribution_cents,
            "contribution_per_order": self.contribution_per_order,
            "refund_at_risk_cents": self.refund_at_risk_cents,
            "refund_eligible_cents": self.refund_eligible_cents,
            "realized_components": self.realized_components,
            "unfulfilled_components": self.unfulfilled_components,
            "mature_orders": self.mature_orders,
            "open_window_orders": self.open_window_orders,
            "basis": self.basis,
        }


def unit_economics(
    view: EconomicView,
    offer: str,
    *,
    mature: Optional[bool] = None,
    purchase_months: Optional[int] = None,
    sales_cutoff: Optional[date] = None,
) -> UnitEconomics:
    """Observed economics for one offer.

    ``purchase_months`` lets a caller annualise a rate from the offer's whole
    sales life while restricting the money figures to an observed cohort.
    When omitted, the rate is based on the same orders as the money.

    ``sales_cutoff`` restricts the *demand rate* to orders already sold by that
    date, so a forward projection does not read next month's bookings back into
    today's decision.  Ticket value still comes from the product's full order
    set, because price and per-delivery cost are catalog properties rather than
    demand evidence.
    """

    groups = view.group(("offer",), mature=mature)
    match = next((g for g in groups if g.key[0] == offer), None)
    orders = [o for o in view.orders if o.offer == offer]
    if mature is not None:
        orders = [o for o in orders if o.mature == mature]
    if purchase_months is None:
        purchase_months = len({_period(o.order.purchase_date) for o in orders})
    if match is None:
        return UnitEconomics(
            offer=offer,
            orders=0,
            purchase_months=0,
            orders_per_month=0.0,
            capture_cents=0,
            net_cents=0,
            recognized_cents=0,
            deferred_cents=0,
            variable_cost_cents=0,
            contribution_cents=0,
            expected_contribution_cents=0,
            contribution_per_order=0.0,
            refund_at_risk_cents=0,
            refund_eligible_cents=0,
            realized_components=0,
            unfulfilled_components=0,
            mature_orders=0,
            open_window_orders=0,
            basis="no observed orders",
        )
    sale_orders = orders
    if sales_cutoff is not None:
        sale_orders = [o for o in orders if o.order.purchase_date <= sales_cutoff]
    sale_months = len({_period(o.order.purchase_date) for o in sale_orders})
    if purchase_months is None:
        purchase_months = sale_months
    per_month = len(sale_orders) / purchase_months if purchase_months else 0.0
    expected = sum(
        line.expected_contribution_cents
        for order in orders
        for line in order.components
    )
    refund_eligible = sum(
        line.refund_eligible_cents
        for order in orders
        for line in order.components
    )
    per_order = expected / match.orders if match.orders else 0.0
    return UnitEconomics(
        offer=offer,
        orders=match.orders,
        purchase_months=purchase_months,
        orders_per_month=per_month,
        capture_cents=match.capture_cents,
        net_cents=match.net_cents,
        recognized_cents=match.recognized_cents,
        deferred_cents=match.deferred_cents,
        variable_cost_cents=match.variable_cost_cents,
        contribution_cents=match.contribution_cents,
        expected_contribution_cents=expected,
        contribution_per_order=per_order,
        refund_at_risk_cents=match.refund_at_risk_cents,
        refund_eligible_cents=refund_eligible,
        realized_components=match.realized_components,
        unfulfilled_components=match.unfulfilled_components,
        mature_orders=sum(1 for o in view.orders if o.offer == offer and o.mature),
        open_window_orders=sum(
            1 for o in view.orders if o.offer == offer and not o.mature
        ),
        basis="observed orders",
    )


def incurred_fixed(
    ds: Dataset, view: EconomicView
) -> Tuple[FixedTermMonth, ...]:
    """Fixed cost actually incurred, by supplier-month with deliveries."""

    months = sorted(
        {line.service_period for line in view.component_lines() if line.service_period}
    )
    deliveries: Dict[Tuple[str, str, str], int] = {}
    for line in view.component_lines():
        if line.redeemed_state and line.service_period:
            key = (line.service_period, line.supplier, line.component)
            deliveries[key] = deliveries.get(key, 0) + 1
    out: List[FixedTermMonth] = []
    for term in ds.supplier_terms:
        for month in months:
            if not term_active_in_month(term, month):
                continue
            count = deliveries.get((month, term.supplier, term.component), 0)
            if not count:
                continue
            out.append(
                FixedTermMonth(
                    month=month,
                    term_id=term.term_id,
                    supplier=term.supplier,
                    component=term.component,
                    cents=term.fixed_cents,
                    avoidable=term.avoidable_if_stopped,
                    status="incurred",
                )
            )
    return tuple(out)


def fixed_plan(
    ds: Dataset,
    months: Sequence[str],
    *,
    active_offers: Sequence[str],
) -> Tuple[FixedTermMonth, ...]:
    """Fixed commitments payable in ``months`` for a forward configuration.

    A term is dropped only when the contract allows avoidance *and* no offer
    still on sale uses that supplier/component.  A contract already cancelled
    contributes nothing from the month after its cancellation.
    """

    active_pairs = _offer_supplier_pairs(
        [row for row in ds.offers if row.offer in set(active_offers)]
    )
    pairs_in_use = {(supplier, component) for supplier, component, _ in active_pairs}
    out: List[FixedTermMonth] = []
    for term in ds.supplier_terms:
        for month in months:
            if not term_active_in_month(term, month):
                continue
            if not term_due_in_month(term, month):
                continue
            pair = (term.supplier, term.component)
            shared = tuple(
                sorted(
                    {
                        offer
                        for supplier, component, offer in active_pairs
                        if (supplier, component) == pair
                    }
                )
            )
            # An avoidable commitment vanishes only when nothing still on sale
            # needs that supplier on that component.  A non-avoidable one stays.
            if term.avoidable_if_stopped and pair not in pairs_in_use:
                status = "avoided"
            else:
                status = "committed"
            out.append(
                FixedTermMonth(
                    month=month,
                    term_id=term.term_id,
                    supplier=term.supplier,
                    component=term.component,
                    cents=term.fixed_cents,
                    avoidable=term.avoidable_if_stopped,
                    status=status,
                    shared_with=shared,
                )
            )
    return tuple(out)


@dataclass
class Scenario:
    name: str
    active_offers: Tuple[str, ...]
    monthly_orders: Dict[str, float]
    months: Tuple[str, ...]
    unit: Dict[str, UnitEconomics]
    variable_contribution_cents: int
    fixed_cents: int
    net_monthly_cents: int
    fixed_detail: Tuple[FixedTermMonth, ...]
    carried_deferred_cents: int
    carried_refund_risk_cents: int
    notes: Tuple[str, ...] = ()
    stopped_offers: Tuple[str, ...] = ()
    units_per_month: float = 0.0
    capacity_cost_cents: int = 0
    expected_contribution_cents: int = 0

    @property
    def cost_per_ticket_cents(self) -> int:
        if not self.units_per_month:
            return 0
        return int(round(self.capacity_cost_cents / self.units_per_month))

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "active_offers": list(self.active_offers),
            "stopped_offers": list(self.stopped_offers),
            "monthly_orders": dict(self.monthly_orders),
            "months": list(self.months),
            "variable_contribution_cents": self.variable_contribution_cents,
            "fixed_cents": self.fixed_cents,
            "net_monthly_cents": self.net_monthly_cents,
            "carried_deferred_cents": self.carried_deferred_cents,
            "carried_refund_risk_cents": self.carried_refund_risk_cents,
            "fixed_detail": [item.as_dict() for item in self.fixed_detail],
            "notes": list(self.notes),
            "units_per_month": self.units_per_month,
            "capacity_cost_cents": self.capacity_cost_cents,
            "expected_contribution_cents": self.expected_contribution_cents,
            "cost_per_ticket_cents": self.cost_per_ticket_cents,
        }


def build_scenario(
    ds: Dataset,
    view: EconomicView,
    name: str,
    *,
    as_of: date,
    active_offers: Sequence[str],
    monthly_orders: Dict[str, float],
    stopped_offers: Sequence[str] = (),
    months: Optional[Sequence[str]] = None,
    notes: Sequence[str] = (),
) -> Scenario:
    """Build one forward scenario.

    ``active_offers`` is what remains *on sale*.  ``stopped_offers`` is the
    subset whose forward sales end; its already-sold obligations are carried
    separately.  An offer kept on sale still carries its own unfulfilled
    entitlements, so stopping one offer must not silently drop another's
    obligations from the comparison.
    """

    horizon = tuple(months) if months is not None else forward_months(as_of)
    unit = {offer: unit_economics(view, offer) for offer in set(active_offers) | set(stopped_offers)}
    variable = sum(
        monthly_orders.get(offer, 0.0) * unit[offer].contribution_per_order
        for offer in active_offers
    )
    fixed = fixed_plan(ds, horizon, active_offers=active_offers)
    monthly_fixed = (
        sum(item.cents for item in fixed if item.status == "committed")
        / max(len(horizon), 1)
    )
    # Only the stopped offer's obligations move into the "still to honour"
    # bucket; an offer that stays on sale carries them within its own run-rate.
    carry = sorted(set(stopped_offers))
    carried_deferred = sum(unit[offer].deferred_cents for offer in carry)
    # Obligations and refund exposure do not end when the observation window
    # closes: an unused, refundable entitlement is still owed.  Carry the wider
    # figure so a mature calendar date cannot hide it.
    carried_risk = sum(unit[offer].refund_eligible_cents for offer in carry)
    units_per_month = sum(
        monthly_orders.get(offer, 0.0) for offer in active_offers
    )
    # Capacity cost is exactly the committed fixed cost of the first forward
    # month, so it always agrees with ``fixed_cents``.  Unavoidable contracts
    # therefore still burden a stopped offer.
    first_month = {item.term_id: item for item in fixed if item.month == horizon[0]}
    capacity_cost = sum(
        item.cents for item in first_month.values() if item.status == "committed"
    )
    expected_contribution = sum(
        monthly_orders.get(offer, 0.0) * unit[offer].contribution_per_order
        for offer in active_offers
    )
    return Scenario(
        name=name,
        active_offers=tuple(active_offers),
        stopped_offers=tuple(stopped_offers),
        monthly_orders=dict(monthly_orders),
        months=horizon,
        unit=unit,
        variable_contribution_cents=int(round(variable)),
        fixed_cents=int(round(monthly_fixed)),
        net_monthly_cents=int(round(variable - monthly_fixed)),
        fixed_detail=fixed,
        carried_deferred_cents=carried_deferred,
        carried_refund_risk_cents=carried_risk,
        notes=tuple(notes),
        units_per_month=units_per_month,
        capacity_cost_cents=capacity_cost,
        expected_contribution_cents=int(round(expected_contribution)),
    )


@dataclass(frozen=True)
class Comparison:
    left: str
    right: str
    delta_monthly_cents: int
    avoidable_fixed_saved_cents: int
    pending: bool
    missing_evidence: Tuple[str, ...]
    suspense_cents: int
    note: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "left": self.left,
            "right": self.right,
            "delta_monthly_cents": self.delta_monthly_cents,
            "avoidable_fixed_saved_cents": self.avoidable_fixed_saved_cents,
            "pending": self.pending,
            "missing_evidence": list(self.missing_evidence),
            "suspense_cents": self.suspense_cents,
            "note": self.note,
        }


def compare_scenarios(
    left: Scenario,
    right: Scenario,
    *,
    suspense_claims: Sequence[Tuple[str, int]] = (),
) -> Comparison:
    """Compare two scenarios, flagging a result that unclaimed cash could flip.

    ``suspense_claims`` is ``(claim, cents)`` for refund cash whose component
    attribution is still missing.  If that total is at least the absolute
    monthly gap, the comparison is not final.
    """

    delta = left.net_monthly_cents - right.net_monthly_cents
    left_terms = {(t.term_id, t.month): t for t in left.fixed_detail}
    right_terms = {(t.term_id, t.month): t for t in right.fixed_detail}
    saved_total = 0
    for key in set(left_terms) & set(right_terms):
        if left_terms[key].status == "committed" and right_terms[key].status == "avoided":
            saved_total += left_terms[key].cents
    saved = round(saved_total / max(len(left.months), 1))
    suspense = sum(cents for _, cents in suspense_claims)
    pending = suspense > 0 and suspense >= abs(delta)
    if suspense and pending:
        note = (
            f"待认领 {suspense} 不小于月差额 {abs(delta)}，"
            "认领结果可能翻转排名"
        )
    elif suspense:
        note = (
            f"待认领 {suspense} 低于月差额 {abs(delta)}，排名暂稳，"
            "仍需单独披露并关注认领结果"
        )
    else:
        note = "退款现金均已归属，排名可定"
    return Comparison(
        left=left.name,
        right=right.name,
        delta_monthly_cents=delta,
        avoidable_fixed_saved_cents=saved,
        pending=pending,
        missing_evidence=tuple(claim for claim, cents in suspense_claims if cents),
        suspense_cents=suspense,
        note=note,
    )


def decision_table(
    ds: Dataset,
    view: EconomicView,
    as_of: date,
    *,
    months: Optional[Sequence[str]] = None,
    demand_transfer: bool = True,
) -> Tuple[Scenario, ...]:
    """Keep-P2 versus stop-P2 versus replace-P2-with-P3.

    Demand for the replacement is assumed to transfer one-for-one from the
    stopped offer; that is an explicit scenario assumption, not observed data.
    """

    horizon = tuple(months) if months is not None else forward_months(as_of)
    # Demand rates use only orders already sold by the decision date.  Ticket
    # value uses each offer's full order set, because price and per-delivery
    # cost are product properties rather than demand evidence.
    def value(offer: str) -> UnitEconomics:
        return unit_economics(view, offer, sales_cutoff=as_of)

    p2 = value("P2")
    p3 = value("P3")
    p2_rate = p2.orders_per_month or 0.0
    p3_rate = p3.orders_per_month or 0.0
    # P2 is the offer under review.  P3 is already on sale and would remain on
    # sale in every scenario, so its pre-decision run-rate is the baseline.
    p3_baseline = p3_rate
    common = (
        "demand transfers one-for-one from the stopped offer to the replacement"
        if demand_transfer
        else "replacement keeps only its own observed order rate"
    )
    scenarios = [
        build_scenario(
            ds,
            view,
            "keep_P2",
            as_of=as_of,
            active_offers=("P2", "P3"),
            monthly_orders={"P2": p2_rate, "P3": p3_baseline},
            months=horizon,
            notes=(
                "P2 and P3 both remain on sale; nothing is stopped so no "
                "obligation is carried separately",
            ),
        ),
        build_scenario(
            ds,
            view,
            "stop_P2_no_replacement",
            as_of=as_of,
            active_offers=("P3",),
            monthly_orders={"P3": p3_baseline},
            stopped_offers=("P2",),
            months=horizon,
            notes=(
                "P2 forward sales end; P3 stays on sale; P2 tickets already "
                "sold remain an obligation",
                f"P3 pre-decision demand evidence: {p3_rate:.1f} tickets/month",
            ),
        ),
        build_scenario(
            ds,
            view,
            "replace_P2_with_P3",
            as_of=as_of,
            active_offers=("P3",),
            monthly_orders={"P3": p3_baseline + (p2_rate if demand_transfer else 0.0)},
            stopped_offers=("P2",),
            months=horizon,
            notes=(
                "P3 takes over demand from P2; P2 tickets already sold are still honoured",
                *common,
                f"transferred volume: {p2_rate:.1f} tickets/month",
            ),
        ),
    ]
    return tuple(scenarios)


def open_suspense_claims(view: EconomicView) -> Tuple[Tuple[str, int], ...]:
    return tuple(
        (allocation.claim, allocation.unallocated_cents)
        for allocation in view.refunds()
        if allocation.unallocated_cents
    )


def allocated_incurred_fixed(
    ds: Dataset, view: EconomicView
) -> Tuple[Dict[str, int], Dict[str, Tuple[str, ...]]]:
    """Spread fixed cost actually incurred across the offers that used it.

    A supplier-month commitment is split among the offers whose delivered
    components relied on that supplier in that month, pro rata by deliveries.
    Shared workshops therefore burden both offers and cannot be counted as
    saved twice.
    """

    deliveries: Dict[Tuple[str, str, str], int] = {}
    by_offer: Dict[Tuple[str, str, str, str], int] = {}
    for line in view.component_lines():
        if line.component == UNALLOCATED:
            continue
        if not line.redeemed_state or line.service_period is None:
            continue
        key = (line.service_period, line.supplier, line.component)
        deliveries[key] = deliveries.get(key, 0) + 1
        okey = key + (line.offer,)
        by_offer[okey] = by_offer.get(okey, 0) + 1

    allocated: Dict[str, int] = {}
    sources: Dict[str, List[str]] = {}
    for term in ds.supplier_terms:
        for (month, supplier, component), count in deliveries.items():
            if supplier != term.supplier or component != term.component:
                continue
            if not term_active_in_month(term, month) or not term_due_in_month(term, month):
                continue
            shares = [
                (offer, units)
                for (m, s, c, offer), units in by_offer.items()
                if (m, s, c) == (month, supplier, component)
            ]
            if not shares:
                continue
            split = allocate_weighted(term.fixed_cents, shares)
            for offer, cents in split.items():
                allocated[offer] = allocated.get(offer, 0) + cents
                sources.setdefault(offer, []).append(
                    f"{term.term_id}/{month}"
                )
    return allocated, {k: tuple(v) for k, v in sources.items()}
