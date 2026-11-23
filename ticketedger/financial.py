"""Period ledger: cash, discounts, refunds, reclasses and sealed books.

The financial view answers a different question from the operating view: *when*
was money booked, *which* period was written down, and what did the sealed
books already recognise.

It deliberately keeps two bases apart:

* **cash basis** — captures, issuer discounts, adjustments, refunds and the
  suspense movements that follow a refund whose attribution was not known when
  the cash landed;
* **recognition basis** — revenue and variable cost only for service delivered
  in a period that is still open.  Sealed months keep their archive amounts and
  are never rewritten.

A later dispute outcome is booked into the first open period, carrying the
original sale and refund period as ``origin_period``.  Suspense is released and
the refund is re-issued against the named components, so the reclass has zero
net cash effect while moving the attribution out of the period that could not
support it.

Sign convention: positive increases the alliance's net cash position, negative
reduces it.  Platform-funded discounts are memo lines (``memo=True``) and never
enter a total; they do not reduce the distributable pool.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .allocation import EconomicView
from .model import Book, Dataset, Money
from .refunds import RefundAllocation


def period_of(day: date) -> str:
    return day.strftime("%Y-%m")


def period_end(period: str) -> date:
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def next_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


@dataclass(frozen=True)
class LedgerLine:
    line_id: str
    period: str
    origin_period: str
    order_id: str
    offer: str
    component: str
    account: str
    amount_cents: int
    funder: str
    reference: str
    source_kind: str
    closed: bool = False
    memo: bool = False
    note: str = ""

    @property
    def effective_cents(self) -> int:
        return 0 if self.memo else self.amount_cents


@dataclass(frozen=True)
class ArchiveMatch:
    book_id: str
    period: str
    source: str
    entry_scope: str
    posted_cents: int
    expected_cents: Optional[int]
    difference_cents: Optional[int]
    matched: bool
    note: str


@dataclass(frozen=True)
class SuspenseItem:
    claim: str
    order_id: str
    offer: str
    amount_cents: int
    refund_entry_ids: Tuple[str, ...]
    booked_period: str
    released_period: Optional[str]
    capture_entry_id: str
    capture_cents: int
    resolved_cents: int
    status: str
    reason: str
    evidence: Tuple[str, ...]


@dataclass(frozen=True)
class PeriodSummary:
    period: str
    state: str
    cash_capture_cents: int
    issuer_discount_cents: int
    platform_subsidy_cents: int
    adjustment_cents: int
    refund_cents: int
    suspense_delta_cents: int
    cash_basis_net_cents: int
    revenue_cents: int
    variable_cost_cents: int
    recognition_contribution_cents: int
    sealed_archive_cents: int
    lines: int

    def as_dict(self) -> Dict[str, object]:
        return {
            "period": self.period,
            "state": self.state,
            "cash_capture_cents": self.cash_capture_cents,
            "issuer_discount_cents": self.issuer_discount_cents,
            "platform_subsidy_cents": self.platform_subsidy_cents,
            "adjustment_cents": self.adjustment_cents,
            "refund_cents": self.refund_cents,
            "suspense_delta_cents": self.suspense_delta_cents,
            "cash_basis_net_cents": self.cash_basis_net_cents,
            "revenue_cents": self.revenue_cents,
            "variable_cost_cents": self.variable_cost_cents,
            "recognition_contribution_cents": self.recognition_contribution_cents,
            "sealed_archive_cents": self.sealed_archive_cents,
            "lines": self.lines,
        }


class FinancialLedger:
    def __init__(self, ds: Dataset, view: EconomicView, as_of: date) -> None:
        self.ds = ds
        self.view = view
        self.as_of = as_of
        self.books: Dict[str, Book] = {book.period: book for book in ds.books}
        self.lines: List[LedgerLine] = []
        self._counter = 0
        self.suspense_items: List[SuspenseItem] = []
        self._sealed_source: Dict[str, List[Money]] = {}
        self._build()

    # -- helpers ---------------------------------------------------------
    def _new_line(self, **kwargs: object) -> LedgerLine:
        self._counter += 1
        kwargs.setdefault("line_id", f"GL{self._counter:04d}")
        line = LedgerLine(**kwargs)  # type: ignore[arg-type]
        self.lines.append(line)
        return line

    def book_state(self, period: str) -> str:
        book = self.books.get(period)
        return book.state if book else "open"

    def is_closed(self, period: str) -> bool:
        return self.book_state(period) == "closed"

    def first_open_on_or_after(self, period: str) -> str:
        candidate = period
        for _ in range(240):
            if not self.is_closed(candidate):
                return candidate
            candidate = next_period(candidate)
        raise ValueError(f"no open period after {period}")

    # -- build -----------------------------------------------------------
    def _build(self) -> None:
        self._archive_lines()
        self._money_lines()
        self._reclass_lines()
        self._recognition_lines()
        self.lines.sort(key=lambda line: (line.period, line.closed, line.line_id))
        self._suspense_items()

    def _archive_lines(self) -> None:
        for entry in self.ds.book_entries:
            self._new_line(
                line_id=f"BK:{entry.book_id}",
                period=entry.period,
                origin_period=entry.period,
                order_id="",
                offer="",
                component="",
                account=f"archive:{entry.entry_scope}",
                amount_cents=entry.posted_cents,
                funder="",
                reference=entry.book_id,
                source_kind="archive",
                closed=True,
                note=f"{entry.source} 的关账归档摘录",
            )

    def _money_lines(self) -> None:
        allocations = {a.claim: a for a in self.view.refunds()}
        for entry in self.ds.money:
            period = period_of(entry.received_date)
            if self.is_closed(period):
                # The sealed book excerpt is authoritative for this period.
                self._sealed_source.setdefault(period, []).append(entry)
                continue
            order = self.ds.order(entry.order_id)
            if entry.kind == "capture":
                self._new_line(
                    period=period,
                    origin_period=period,
                    order_id=entry.order_id,
                    offer=order.offer,
                    component="",
                    account="cash_capture",
                    amount_cents=entry.amount_cents,
                    funder="",
                    reference=entry.entry_id,
                    source_kind="capture",
                )
            elif entry.kind == "whole_coupon":
                if entry.funder == "issuer":
                    self._new_line(
                        period=period,
                        origin_period=period,
                        order_id=entry.order_id,
                        offer=order.offer,
                        component="",
                        account="issuer_discount",
                        amount_cents=-entry.amount_cents,
                        funder=entry.funder,
                        reference=entry.entry_id,
                        source_kind="whole_coupon",
                        note="发行方出资的整单红包冲减可分金额",
                    )
                else:
                    self._new_line(
                        period=period,
                        origin_period=period,
                        order_id=entry.order_id,
                        offer=order.offer,
                        component="",
                        account="platform_subsidy",
                        amount_cents=entry.amount_cents,
                        funder=entry.funder,
                        reference=entry.entry_id,
                        source_kind="whole_coupon",
                        memo=True,
                        note="平台出资补贴：仅备忘，不重复冲减",
                    )
            elif entry.kind == "price_adjustment":
                self._new_line(
                    period=period,
                    origin_period=period,
                    order_id=entry.order_id,
                    offer=order.offer,
                    component=entry.component,
                    account="price_adjustment",
                    amount_cents=-entry.amount_cents,
                    funder=entry.funder,
                    reference=entry.entry_id,
                    source_kind="price_adjustment",
                )
            elif entry.kind == "refund":
                self._refund_cash_lines(
                    entry, period, order.offer, allocations[entry.claim]
                )
            else:  # pragma: no cover - validated upstream
                raise ValueError(entry.kind)

    def _refund_cash_lines(
        self, entry: Money, period: str, offer: str, allocation: RefundAllocation
    ) -> None:
        final_period = (
            period_of(allocation.final_resolution)
            if allocation.final_resolution
            else None
        )
        if final_period == period:
            # Attribution is already final when the cash lands: split at once.
            self._split_refund(
                entry,
                period,
                offer,
                allocation,
                source_kind="refund",
                origin_period=period,
            )
            return
        # Evidence arrives later (or never): hold the whole cash refund in
        # suspense; nothing is pushed onto a component on a guess.
        self._new_line(
            period=period,
            origin_period=period,
            order_id=entry.order_id,
            offer=offer,
            component="unallocated",
            account="refund_suspense",
            amount_cents=-entry.amount_cents,
            funder="",
            reference=entry.entry_id,
            source_kind="refund",
            note="等待最终回执，暂列待认领",
        )

    def _split_refund(
        self,
        entry: Money,
        period: str,
        offer: str,
        allocation: RefundAllocation,
        *,
        source_kind: str,
        origin_period: str,
    ) -> None:
        for component, item in allocation.components.items():
            self._new_line(
                period=period,
                origin_period=origin_period,
                order_id=entry.order_id,
                offer=offer,
                component=component,
                account="refund",
                amount_cents=-item.cents,
                funder="",
                reference=",".join((entry.entry_id,) + item.receipt_ids),
                source_kind=source_kind,
                note=(
                    f"退款现金 {entry.entry_id} 按"
                    f"{'最终回执' if item.basis == 'receipt' else '渠道明示 component'}认领"
                ),
            )
        if allocation.unallocated_cents:
            self._new_line(
                period=period,
                origin_period=origin_period,
                order_id=entry.order_id,
                offer=offer,
                component="unallocated",
                account="refund_suspense",
                amount_cents=-allocation.unallocated_cents,
                funder="",
                reference=entry.entry_id,
                source_kind=source_kind,
                note="收款渠道尚未确认归属，留在待认领",
            )

    def _reclass_lines(self) -> None:
        for allocation in self.view.refunds():
            if not allocation.final_resolution or allocation.refund_period is None:
                continue
            resolution_period = period_of(allocation.final_resolution)
            receipt_period = allocation.refund_period
            if resolution_period == receipt_period:
                continue
            if self.is_closed(resolution_period):
                # The resolution itself is sealed; nothing to post today.
                continue
            target = self.first_open_on_or_after(resolution_period)
            if target == receipt_period:
                continue
            order = self.ds.order(allocation.order_id)
            resolved = allocation.allocated_cents
            if resolved:
                self._new_line(
                    period=target,
                    origin_period=receipt_period,
                    order_id=allocation.order_id,
                    offer=order.offer,
                    component="unallocated",
                    account="suspense_release",
                    amount_cents=resolved,
                    funder="",
                    reference=",".join(allocation.refund_entry_ids),
                    source_kind="dispute_reclass",
                    note=(
                        f"从待认领释放 {resolved}；退款现金原入账 {receipt_period}，"
                        f"最终回执 {allocation.final_resolution.isoformat()}"
                    ),
                )
            for component, item in allocation.components.items():
                self._new_line(
                    period=target,
                    origin_period=receipt_period,
                    order_id=allocation.order_id,
                    offer=order.offer,
                    component=component,
                    account="refund",
                    amount_cents=-item.cents,
                    funder="",
                    reference=",".join(item.receipt_ids),
                    source_kind="dispute_reclass",
                    note=(
                        f"最终回执 {', '.join(item.receipt_ids)}；退款现金原入账 "
                        f"{receipt_period}；原销售期 "
                        f"{_sale_period(self.ds, allocation.order_id)}"
                    ),
                )

    def _recognition_lines(self) -> None:
        """Revenue and variable cost for service delivered in open periods only."""

        for order in self.view.orders:
            for line in order.components:
                if not line.redeemed_state or line.service_period is None:
                    # Not delivered by the decision date: stays an obligation.
                    continue
                period = line.service_period
                if self.is_closed(period):
                    continue
                self._new_line(
                    period=period,
                    origin_period=line.purchase_period,
                    order_id=line.order_id,
                    offer=line.offer,
                    component=line.component,
                    account="revenue",
                    amount_cents=line.recognized_cents,
                    funder="",
                    reference=line.voucher_id,
                    source_kind="recognition",
                    note=(
                        f"由 {line.supplier} 实际履约"
                        + (
                            f"（售出版本为 {line.sold_supplier}）"
                            if line.supplier_changed
                            else ""
                        )
                    ),
                )
                self._new_line(
                    period=period,
                    origin_period=line.purchase_period,
                    order_id=line.order_id,
                    offer=line.offer,
                    component=line.component,
                    account="variable_cost",
                    amount_cents=-line.variable_cost_cents,
                    funder=line.supplier,
                    reference=line.voucher_id,
                    source_kind="recognition",
                    note=f"{line.supplier} 的按次成本",
                )

    def _suspense_items(self) -> None:
        for allocation in self.view.refunds():
            order = self.ds.order(allocation.order_id)
            capture = next(
                (
                    m
                    for m in self.ds.money
                    if m.kind == "capture" and m.order_id == allocation.order_id
                ),
                None,
            )
            if allocation.unallocated_cents == 0:
                status = "attributed"
                reason = "最终回执或收款渠道已明确每个子权益"
            elif allocation.allocated_cents:
                status = "partially_attributed"
                reason = allocation.notes[-1] if allocation.notes else "证据只覆盖部分退款"
            else:
                status = "open"
                reason = allocation.notes[-1] if allocation.notes else "没有可归属凭证"
            released: Optional[str] = None
            if allocation.final_resolution:
                released = self.first_open_on_or_after(
                    period_of(allocation.final_resolution)
                )
            self.suspense_items.append(
                SuspenseItem(
                    claim=allocation.claim,
                    order_id=allocation.order_id,
                    offer=order.offer,
                    amount_cents=allocation.unallocated_cents,
                    refund_entry_ids=allocation.refund_entry_ids,
                    booked_period=allocation.refund_period or "",
                    released_period=released,
                    capture_entry_id=capture.entry_id if capture else "",
                    capture_cents=capture.amount_cents if capture else 0,
                    resolved_cents=allocation.allocated_cents,
                    status=status,
                    reason=reason,
                    evidence=tuple(
                        receipt.receipt
                        for receipt in allocation.receipts
                        if receipt.is_final
                    ),
                )
            )

    # -- accessors -------------------------------------------------------
    def journal_for(self, period: str) -> Tuple[LedgerLine, ...]:
        return tuple(line for line in self.lines if line.period == period)

    def open_period_journal(self, period: str) -> Tuple[LedgerLine, ...]:
        return tuple(
            line
            for line in self.lines
            if line.period == period and not line.closed and not line.memo
        )

    def lines_for_order(self, order_id: str) -> Tuple[LedgerLine, ...]:
        return tuple(line for line in self.lines if line.order_id == order_id)

    def open_periods(self) -> Tuple[str, ...]:
        periods = {p for p, b in self.books.items() if b.state == "open"}
        periods |= {line.period for line in self.lines if not line.closed}
        return tuple(sorted(periods))

    def period_summary(self) -> Tuple[PeriodSummary, ...]:
        periods = sorted({line.period for line in self.lines} | set(self.books))
        out: List[PeriodSummary] = []
        for period in periods:
            lines = [line for line in self.journal_for(period) if not line.memo]
            sums: Dict[str, int] = {}
            for line in lines:
                sums[line.account] = sums.get(line.account, 0) + line.amount_cents
            capture = sums.get("cash_capture", 0)
            issuer = sums.get("issuer_discount", 0)
            platform = sum(
                line.amount_cents
                for line in self.journal_for(period)
                if line.account == "platform_subsidy"
            )
            adjustment = sums.get("price_adjustment", 0)
            refund = sums.get("refund", 0)
            suspense = sums.get("refund_suspense", 0) + sums.get("suspense_release", 0)
            revenue = sums.get("revenue", 0)
            variable = sums.get("variable_cost", 0)
            archive = sums.get(
                "archive:receipts",
                0,
            ) + sums.get("archive:issuer_discount", 0) + sums.get(
                "archive:unresolved", 0
            )
            out.append(
                PeriodSummary(
                    period=period,
                    state=self.book_state(period),
                    cash_capture_cents=capture,
                    issuer_discount_cents=issuer,
                    platform_subsidy_cents=platform,
                    adjustment_cents=adjustment,
                    refund_cents=refund,
                    suspense_delta_cents=suspense,
                    cash_basis_net_cents=(
                        capture + issuer + adjustment + refund + suspense
                    ),
                    revenue_cents=revenue,
                    variable_cost_cents=variable,
                    recognition_contribution_cents=revenue + variable,
                    sealed_archive_cents=archive,
                    lines=len(lines),
                )
            )
        return tuple(out)

    def archive_reconciliation(self) -> Tuple[ArchiveMatch, ...]:
        money = {m.entry_id: m for m in self.ds.money}
        out: List[ArchiveMatch] = []
        for entry in self.ds.book_entries:
            source = money.get(entry.source)
            if source is None:
                out.append(
                    ArchiveMatch(
                        entry.book_id,
                        entry.period,
                        entry.source,
                        entry.entry_scope,
                        entry.posted_cents,
                        None,
                        None,
                        False,
                        "源凭证不在提供的记录中",
                    )
                )
                continue
            expected = _expected_archive_cents(source)
            difference = entry.posted_cents - expected
            if difference != 0:
                note = "归档金额与源凭证推导不一致"
            elif entry.entry_scope == "unresolved":
                note = (
                    "关账时列为未归属；后续最终回执在开放期重新认领，"
                    "关账金额不变"
                )
            elif entry.entry_scope == "issuer_discount":
                note = "发行方整单红包冲减可分金额"
            else:
                note = "与源凭证一致"
            out.append(
                ArchiveMatch(
                    entry.book_id,
                    entry.period,
                    entry.source,
                    entry.entry_scope,
                    entry.posted_cents,
                    expected,
                    difference,
                    difference == 0,
                    note,
                )
            )
        return tuple(out)

    def sealed_period_source_activity(self) -> Dict[str, Tuple[Money, ...]]:
        """Source rows already sealed, kept only as a cross-check."""

        return {p: tuple(rows) for p, rows in sorted(self._sealed_source.items())}

    def suspense_report(self) -> Tuple[SuspenseItem, ...]:
        return tuple(self.suspense_items)

    def open_suspense(self) -> int:
        return sum(
            item.amount_cents
            for item in self.suspense_items
            if item.status != "attributed"
        )

    def suspense_for_offer(self, offer: str) -> int:
        return sum(
            item.amount_cents
            for item in self.suspense_items
            if item.offer == offer and item.status != "attributed"
        )


def _expected_archive_cents(entry: Money) -> int:
    if entry.kind in ("capture",):
        return entry.amount_cents
    if entry.kind in ("refund", "price_adjustment"):
        return -entry.amount_cents
    if entry.kind == "whole_coupon" and entry.funder == "issuer":
        return -entry.amount_cents
    if entry.kind == "whole_coupon":  # platform memo
        return 0
    raise ValueError(entry.kind)


def _sale_period(ds: Dataset, order_id: str) -> str:
    return period_of(ds.order(order_id).purchase_date)
