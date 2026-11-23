"""Loaded, validated source records.

Every CSV row becomes a small dict-like record with integer amounts and
``datetime.date`` dates.  The loader is strict: unknown offers, suppliers,
components, dangling voucher predecessors and malformed values raise
``DataError`` instead of being silently coerced.  That keeps the downstream
allocation honest about what the source actually supports.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


class DataError(ValueError):
    """Raised when source records cannot be reconciled."""


COMPONENTS = ("hall", "lab", "bus")
REFUND_POLICIES = ("open", "unused", "none")
MONEY_KINDS = (
    "capture",
    "refund",
    "price_adjustment",
    "whole_coupon",
)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataError(f"{path.name}: missing header")
        rows = list(reader)
    for row in rows:
        if any(value is None for value in row.values()):
            raise DataError(f"{path.name}: ragged row {row}")
    return rows


def _parse_date(value: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - exercised via tests
        raise DataError(f"{where}: invalid date {value!r}") from exc


def _parse_int(value: str, where: str) -> int:
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - exercised via tests
        raise DataError(f"{where}: invalid integer {value!r}") from exc


def _optional_date(value: str, where: str) -> date | None:
    return _parse_date(value, where) if value else None


@dataclass(frozen=True)
class OfferComponent:
    offer: str
    component: str
    weight: int
    supplier: str
    refundable: str
    cost_cents: int
    valid_from: date
    valid_through: date


@dataclass(frozen=True)
class Order:
    order_id: str
    offer: str
    household: str
    channel: str
    purchase_date: date
    normal_window_end: date


@dataclass(frozen=True)
class Voucher:
    coupon_id: str
    order_id: str
    component: str
    supplier: str
    guest: str
    previous: str
    scheduled: date | None
    redeemed: date | None
    state: str


@dataclass(frozen=True)
class Money:
    entry_id: str
    order_id: str
    kind: str
    amount_cents: int
    component: str
    claim: str
    funder: str
    event_date: date
    received_date: date


@dataclass(frozen=True)
class Dispute:
    receipt: str
    claim: str
    order_id: str
    component: str
    resolved_cents: int
    result: str
    replacement_of: str
    latest_resolution: date
    recorded_at: date


@dataclass(frozen=True)
class Book:
    period: str
    state: str
    locked_on: date | None


@dataclass(frozen=True)
class BookEntry:
    book_id: str
    period: str
    source: str
    entry_scope: str
    posted_cents: int


@dataclass(frozen=True)
class SupplierTerm:
    term_id: str
    supplier: str
    component: str
    fixed_cents: int
    avoidable_if_stopped: bool
    cancelled_on: date | None


@dataclass(frozen=True)
class CommercialAction:
    action_id: str
    offer: str
    replacement: str
    state: str
    event_date: date


@dataclass
class Dataset:
    """All source tables plus lookup helpers."""

    offers: Tuple[OfferComponent, ...]
    orders: Tuple[Order, ...]
    vouchers: Tuple[Voucher, ...]
    money: Tuple[Money, ...]
    disputes: Tuple[Dispute, ...]
    books: Tuple[Book, ...]
    book_entries: Tuple[BookEntry, ...]
    supplier_terms: Tuple[SupplierTerm, ...]
    commercial_actions: Tuple[CommercialAction, ...]
    _order_by_id: Dict[str, Order] = field(default_factory=dict, repr=False)
    _voucher_by_id: Dict[str, Voucher] = field(default_factory=dict, repr=False)

    # -- lookups ---------------------------------------------------------
    def order(self, order_id: str) -> Order:
        try:
            return self._order_by_id[order_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise DataError(f"unknown order {order_id!r}") from exc

    def voucher(self, coupon_id: str) -> Voucher:
        try:
            return self._voucher_by_id[coupon_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise DataError(f"unknown voucher {coupon_id!r}") from exc

    def offer_row(self, offer: str, component: str) -> OfferComponent:
        for row in self.offers:
            if row.offer == offer and row.component == component:
                return row
        raise DataError(f"unknown offer component {offer}/{component}")

    def offer_components(self, offer: str) -> Tuple[OfferComponent, ...]:
        rows = tuple(row for row in self.offers if row.offer == offer)
        if not rows:
            raise DataError(f"unknown offer {offer!r}")
        return rows

    def components(self, offer: str) -> Tuple[str, ...]:
        return tuple(row.component for row in self.offer_components(offer))

    def component_weight_total(self, offer: str) -> int:
        return sum(row.weight for row in self.offer_components(offer))

    def rate_for(self, offer: str, component: str, supplier: str) -> int:
        """Per-delivery cost rate for the supplier that actually delivered.

        The sold offer version is preferred, so a shared supplier with
        different contract rates per version is priced on the version the
        household actually bought.  Only when a re-issued voucher changed the
        supplier (and the sold version has no row for that supplier) do we
        fall back to the supplier's rate on another version.
        """

        preferred = [
            row.cost_cents
            for row in self.offers
            if row.offer == offer
            and row.component == component
            and row.supplier == supplier
        ]
        if len(set(preferred)) == 1:
            return preferred[0]
        fallback = sorted(
            {
                row.cost_cents
                for row in self.offers
                if row.component == component and row.supplier == supplier
            }
        )
        if len(fallback) == 1:
            return fallback[0]
        if not fallback:
            raise DataError(
                f"no cost rate for {offer}/{component} supplied by {supplier}"
            )
        raise DataError(
            f"ambiguous cost rate for {offer}/{component} supplied by {supplier}: "
            f"{fallback}"
        )

    def purchases(self, offer: str) -> Tuple[Order, ...]:
        return tuple(order for order in self.orders if order.offer == offer)

    def is_closed(self, period: str) -> bool:
        for book in self.books:
            if book.period == period:
                return book.state == "closed"
        raise DataError(f"unknown accounting period {period!r}")

    def captured(self, order_id: str) -> int:
        return sum(
            m.amount_cents
            for m in self.money
            if m.kind == "capture" and m.order_id == order_id
        )

    def replace(self, **changes: Any) -> "Dataset":
        """Copy the dataset with selected tables replaced.

        Lookup caches are rebuilt so a modified table cannot leave stale keys
        behind; this is what the synthetic-evidence tests rely on.
        """

        fields = {
            "offers": self.offers,
            "orders": self.orders,
            "vouchers": self.vouchers,
            "money": self.money,
            "disputes": self.disputes,
            "books": self.books,
            "book_entries": self.book_entries,
            "supplier_terms": self.supplier_terms,
            "commercial_actions": self.commercial_actions,
        }
        unknown = set(changes) - set(fields)
        if unknown:
            raise TypeError(f"unknown dataset fields: {sorted(unknown)}")
        fields.update(changes)
        return Dataset(
            **fields,
            _order_by_id={o.order_id: o for o in fields["orders"]},
            _voucher_by_id={v.coupon_id: v for v in fields["vouchers"]},
        )

    def as_of(self, day: date) -> "Dataset":
        """A decision-time slice: nothing that happened after ``day``.

        Orders, cash, adjustments and dispute receipts dated after the decision
        date are excluded.  Vouchers are kept even when scheduled in the future
        because those are the live performance obligations the decision must
        carry; their redemption date is already checked against ``as_of`` when
        revenue is recognised.  Books are kept whole: sealed periods must stay
        visible regardless of the decision date.
        """

        orders = tuple(o for o in self.orders if o.purchase_date <= day)
        order_ids = {o.order_id for o in orders}
        return self.replace(
            orders=orders,
            vouchers=tuple(v for v in self.vouchers if v.order_id in order_ids),
            money=tuple(
                m
                for m in self.money
                if m.order_id in order_ids
                and m.received_date <= day
                and m.event_date <= day
            ),
            disputes=tuple(d for d in self.disputes if d.recorded_at <= day),
            commercial_actions=tuple(
                a for a in self.commercial_actions if a.event_date <= day
            ),
        )

    def horizon(self) -> date:
        """Latest date actually evidenced anywhere in the source records."""

        dates = [o.purchase_date for o in self.orders]
        dates += [o.normal_window_end for o in self.orders]
        dates += [m.received_date for m in self.money]
        dates += [m.event_date for m in self.money]
        dates += [v.redeemed for v in self.vouchers if v.redeemed]
        dates += [v.scheduled for v in self.vouchers if v.scheduled]
        dates += [d.recorded_at for d in self.disputes]
        dates += [d.latest_resolution for d in self.disputes]
        dates += [b.locked_on for b in self.books if b.locked_on]
        dates += [a.event_date for a in self.commercial_actions]
        return max(dates)


def _as_bool(value: str, where: str) -> bool:
    if value == "yes":
        return True
    if value == "no":
        return False
    raise DataError(f"{where}: expected yes/no, got {value!r}")


def _require_order(ds_orders: Mapping[str, Order], order_id: str, where: str) -> None:
    if order_id not in ds_orders:
        raise DataError(f"{where}: unknown order {order_id!r}")


def load_dataset(root: Path | str = None) -> Dataset:
    """Load and validate every table from ``root/data``."""

    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    data = root / "data"

    offers = tuple(
        OfferComponent(
            offer=r["offer"],
            component=r["component"],
            weight=_parse_int(r["weight"], "offers.weight"),
            supplier=r["supplier"],
            refundable=r["refundable"],
            cost_cents=_parse_int(r["cost_cents"], "offers.cost_cents"),
            valid_from=_parse_date(r["valid_from"], "offers.valid_from"),
            valid_through=_parse_date(r["valid_through"], "offers.valid_through"),
        )
        for r in _read_csv(data / "offers.csv")
    )
    orders = tuple(
        Order(
            order_id=r["order_id"],
            offer=r["offer"],
            household=r["household"],
            channel=r["channel"],
            purchase_date=_parse_date(r["purchase_date"], "orders.purchase_date"),
            normal_window_end=_parse_date(
                r["normal_window_end"], "orders.normal_window_end"
            ),
        )
        for r in _read_csv(data / "orders.csv")
    )
    vouchers = tuple(
        Voucher(
            coupon_id=r["coupon_id"],
            order_id=r["order_id"],
            component=r["component"],
            supplier=r["supplier"],
            guest=r["guest"],
            previous=r["previous"],
            scheduled=_optional_date(r["scheduled"], "vouchers.scheduled"),
            redeemed=_optional_date(r["redeemed"], "vouchers.redeemed"),
            state=r["state"],
        )
        for r in _read_csv(data / "vouchers.csv")
    )
    money = tuple(
        Money(
            entry_id=r["entry_id"],
            order_id=r["order_id"],
            kind=r["kind"],
            amount_cents=_parse_int(r["amount_cents"], "money.amount_cents"),
            component=r["component"],
            claim=r["claim"],
            funder=r["funder"],
            event_date=_parse_date(r["event_date"], "money.event_date"),
            received_date=_parse_date(r["received_date"], "money.received_date"),
        )
        for r in _read_csv(data / "money.csv")
    )
    disputes = tuple(
        Dispute(
            receipt=r["receipt"],
            claim=r["claim"],
            order_id=r["order_id"],
            component=r["component"],
            resolved_cents=_parse_int(r["resolved_cents"], "disputes.resolved_cents"),
            result=r["result"],
            replacement_of=r["replacement_of"],
            latest_resolution=_parse_date(
                r["latest_resolution"], "disputes.latest_resolution"
            ),
            recorded_at=_parse_date(r["recorded_at"], "disputes.recorded_at"),
        )
        for r in _read_csv(data / "disputes.csv")
    )
    books = tuple(
        Book(
            period=r["period"],
            state=r["state"],
            locked_on=_optional_date(r["locked_on"], "books.locked_on"),
        )
        for r in _read_csv(data / "books.csv")
    )
    book_entries = tuple(
        BookEntry(
            book_id=r["book_id"],
            period=r["period"],
            source=r["source"],
            entry_scope=r["entry_scope"],
            posted_cents=_parse_int(r["posted_cents"], "book_entries.posted_cents"),
        )
        for r in _read_csv(data / "book_entries.csv")
    )
    supplier_terms = tuple(
        SupplierTerm(
            term_id=r["term_id"],
            supplier=r["supplier"],
            component=r["component"],
            fixed_cents=_parse_int(r["fixed_cents"], "supplier_terms.fixed_cents"),
            avoidable_if_stopped=_as_bool(
                r["avoidable_if_stopped"], "supplier_terms.avoidable_if_stopped"
            ),
            cancelled_on=_optional_date(
                r["cancelled_on"], "supplier_terms.cancelled_on"
            ),
        )
        for r in _read_csv(data / "supplier_terms.csv")
    )
    commercial_actions = tuple(
        CommercialAction(
            action_id=r["action_id"],
            offer=r["offer"],
            replacement=r["replacement"],
            state=r["state"],
            event_date=_parse_date(r["event_date"], "commercial_actions.event_date"),
        )
        for r in _read_csv(data / "commercial_actions.csv")
    )

    _validate(offers, orders, vouchers, money, disputes, books, supplier_terms)

    order_by_id = {o.order_id: o for o in orders}
    voucher_by_id = {v.coupon_id: v for v in vouchers}
    return Dataset(
        offers=offers,
        orders=orders,
        vouchers=vouchers,
        money=money,
        disputes=disputes,
        books=books,
        book_entries=book_entries,
        supplier_terms=supplier_terms,
        commercial_actions=commercial_actions,
        _order_by_id=order_by_id,
        _voucher_by_id=voucher_by_id,
    )


def _validate(
    offers: Sequence[OfferComponent],
    orders: Sequence[Order],
    vouchers: Sequence[Voucher],
    money: Sequence[Money],
    disputes: Sequence[Dispute],
    books: Sequence[Book],
    supplier_terms: Sequence[SupplierTerm],
) -> None:
    if not offers or not orders:
        raise DataError("offers and orders must be non-empty")

    offer_keys: set[Tuple[str, str]] = set()
    for row in offers:
        if (row.offer, row.component) in offer_keys:
            raise DataError(f"duplicate offer component {row.offer}/{row.component}")
        offer_keys.add((row.offer, row.component))
        if row.component not in COMPONENTS:
            raise DataError(f"unknown component {row.component!r}")
        if row.weight <= 0:
            raise DataError(f"non-positive weight for {row.offer}/{row.component}")
        if row.refundable not in REFUND_POLICIES:
            raise DataError(f"unknown refund policy {row.refundable!r}")
        if row.valid_through < row.valid_from:
            raise DataError(f"inverted validity for {row.offer}/{row.component}")

    offers_by_offer: Dict[str, List[OfferComponent]] = {}
    for row in offers:
        offers_by_offer.setdefault(row.offer, []).append(row)

    order_ids: set[str] = set()
    for order in orders:
        if order.order_id in order_ids:
            raise DataError(f"duplicate order {order.order_id}")
        order_ids.add(order.order_id)
        if order.offer not in offers_by_offer:
            raise DataError(f"order {order.order_id} references unknown offer")
        if order.normal_window_end < order.purchase_date:
            raise DataError(f"order {order.order_id} window ends before purchase")

    voucher_ids = {v.coupon_id for v in vouchers}
    voucher_by_id = {v.coupon_id: v for v in vouchers}
    if len(voucher_ids) != len(vouchers):
        raise DataError("duplicate voucher id")
    for voucher in vouchers:
        if voucher.order_id not in order_ids:
            raise DataError(
                f"voucher {voucher.coupon_id} references unknown order"
            )
        order = next(o for o in orders if o.order_id == voucher.order_id)
        allowed = {row.component: row for row in offers_by_offer[order.offer]}
        if voucher.component not in allowed:
            raise DataError(
                f"voucher {voucher.coupon_id} component not in offer {order.offer}"
            )
        if voucher.previous:
            if voucher.previous not in voucher_ids:
                raise DataError(
                    f"voucher {voucher.coupon_id} has dangling previous"
                )
            source = voucher_by_id[voucher.previous]
            if (
                source.order_id != voucher.order_id
                or source.component != voucher.component
            ):
                raise DataError(
                    f"voucher {voucher.coupon_id} changes entitlement across orders"
                )
        if voucher.state == "redeemed" and voucher.redeemed is None:
            raise DataError(f"redeemed voucher {voucher.coupon_id} lacks date")
        if voucher.state != "redeemed" and voucher.redeemed is not None:
            raise DataError(
                f"voucher {voucher.coupon_id} has a redemption date but state "
                f"{voucher.state!r}"
            )

    # Each order must have exactly one live (non-void, not superseded) leaf
    # voucher per component of its sold offer version.
    superseded = {
        v.previous for v in vouchers if v.previous
    }
    for order in orders:
        expected = {row.component for row in offers_by_offer[order.offer]}
        leaves: Dict[str, List[Voucher]] = {}
        for voucher in vouchers:
            if voucher.order_id != order.order_id:
                continue
            if voucher.coupon_id in superseded:
                continue
            if voucher.state == "void":
                continue
            leaves.setdefault(voucher.component, []).append(voucher)
        for component in expected:
            candidates = leaves.get(component, [])
            if len(candidates) != 1:
                raise DataError(
                    f"order {order.order_id} component {component} has "
                    f"{len(candidates)} live leaves, expected 1"
                )

    for row in money:
        if row.order_id not in order_ids:
            raise DataError(f"money {row.entry_id} references unknown order")
        if row.kind not in MONEY_KINDS:
            raise DataError(f"money {row.entry_id} has unknown kind {row.kind!r}")
        if row.kind == "whole_coupon":
            # Whoever funds a whole-coupon discount decides whether it cuts the
            # alliance's distributable pool (issuer) or is somebody else's
            # subsidy (any other named funder, treated as a memo).
            if not row.funder:
                raise DataError(f"whole_coupon {row.entry_id} lacks a funder")
        if row.kind == "price_adjustment" and not row.funder:
            raise DataError(f"price_adjustment {row.entry_id} lacks a funder")
        if row.kind not in ("whole_coupon", "price_adjustment") and row.funder:
            raise DataError(f"money {row.entry_id} has unexpected funder {row.funder!r}")
        if row.amount_cents <= 0:
            raise DataError(f"money {row.entry_id} must be positive")
        if row.received_date < row.event_date:
            raise DataError(f"money {row.entry_id} received before event")

    receipts = {d.receipt for d in disputes}
    for dispute in disputes:
        if dispute.order_id not in order_ids:
            raise DataError(f"dispute {dispute.receipt} references unknown order")
        if dispute.result not in ("partial", "closed"):
            raise DataError(f"dispute {dispute.receipt} bad result")
        if dispute.resolved_cents <= 0:
            raise DataError(f"dispute {dispute.receipt} must be positive")
        if dispute.replacement_of and dispute.replacement_of not in receipts:
            raise DataError(f"dispute {dispute.receipt} replacement target missing")
        if dispute.replacement_of == dispute.receipt:
            raise DataError(f"dispute {dispute.receipt} replaces itself")

    periods = {b.period for b in books}
    if len(periods) != len(books):
        raise DataError("duplicate accounting period")
    for book in books:
        if book.state not in ("open", "closed"):
            raise DataError(f"book {book.period} has unknown state")
        if book.state == "closed" and book.locked_on is None:
            raise DataError(f"closed book {book.period} lacks lock date")

    for term in supplier_terms:
        if term.component not in COMPONENTS:
            raise DataError(f"term {term.term_id} has unknown component")
        if term.fixed_cents < 0:
            raise DataError(f"term {term.term_id} has negative commitment")


