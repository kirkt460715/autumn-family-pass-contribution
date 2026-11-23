"""Trace one ticket / claim from cash to service and back to the ledger.

Two entry points:

* ``trace_order`` walks a whole order: capture, discount, every component's
  voucher lineage (including a supplier change on re-issue), refund evidence,
  and every ledger line that touched the order;
* ``trace_claim`` walks one refund claim: cash entries, receipt chain,
  superseded vs final receipts, allocation and suspense, the reclass posted to
  a later open period, and which combination's economics the claim affects.

Both return plain dictionaries so they can be printed as JSON or Markdown.
"""

from __future__ import annotations


from datetime import date
from typing import Dict, List

from .allocation import EconomicView, UNALLOCATED
from .financial import FinancialLedger
from .model import Dataset
from .refunds import RefundAllocation
from .scenario import unit_economics


def _money(row) -> Dict[str, object]:
    return {
        "entry_id": row.entry_id,
        "kind": row.kind,
        "amount_cents": row.amount_cents,
        "component": row.component,
        "claim": row.claim,
        "funder": row.funder,
        "event_date": row.event_date.isoformat(),
        "received_date": row.received_date.isoformat(),
    }


def _ledger(row) -> Dict[str, object]:
    return {
        "line_id": row.line_id,
        "period": row.period,
        "origin_period": row.origin_period,
        "account": row.account,
        "component": row.component,
        "amount_cents": row.amount_cents,
        "funder": row.funder,
        "reference": row.reference,
        "source_kind": row.source_kind,
        "closed": row.closed,
        "memo": row.memo,
        "note": row.note,
    }


def _refund(allocation: RefundAllocation) -> Dict[str, object]:
    return {
        "claim": allocation.claim,
        "order_id": allocation.order_id,
        "cash_cents": allocation.cash_cents,
        "refund_entry_ids": list(allocation.refund_entry_ids),
        "refund_period": allocation.refund_period,
        "final_resolution": allocation.final_resolution.isoformat()
        if allocation.final_resolution
        else None,
        "components": {
            component: {
                "cents": item.cents,
                "basis": item.basis,
                "receipts": list(item.receipt_ids),
            }
            for component, item in allocation.components.items()
        },
        "unallocated_cents": allocation.unallocated_cents,
        "receipts": [
            {
                "receipt": trace.receipt,
                "component": trace.component,
                "resolved_cents": trace.resolved_cents,
                "result": trace.result,
                "replacement_of": trace.replacement_of,
                "superseded_by": list(trace.superseded_by),
                "final": trace.is_final,
                "recorded_at": trace.recorded_at.isoformat(),
                "latest_resolution": trace.latest_resolution.isoformat(),
            }
            for trace in allocation.receipts
        ],
        "notes": list(allocation.notes),
    }


def trace_order(
    ds: Dataset, view: EconomicView, ledger: FinancialLedger, order_id: str
) -> Dict[str, object]:
    order_line = view.order_line(order_id)
    order = order_line.order
    captures = [m for m in ds.money if m.order_id == order_id and m.kind == "capture"]
    discounts = [
        m for m in ds.money if m.order_id == order_id and m.kind == "whole_coupon"
    ]
    adjustments = [
        m for m in ds.money if m.order_id == order_id and m.kind == "price_adjustment"
    ]
    components: List[Dict[str, object]] = []
    for line in order_line.components:
        if line.component == UNALLOCATED:
            components.append(
                {
                    "component": UNALLOCATED,
                    "refund_cents": line.refund_cents,
                    "refund_basis": line.refund_basis,
                    "note": "refund cash not attributable with current evidence",
                }
            )
            continue
        voucher = ds.voucher(line.voucher_id)
        vouchers = [
            {
                "coupon_id": vid,
                "supplier": ds.voucher(vid).supplier,
                "guest": ds.voucher(vid).guest,
                "state": ds.voucher(vid).state,
                "scheduled": ds.voucher(vid).scheduled.isoformat()
                if ds.voucher(vid).scheduled
                else None,
                "redeemed": ds.voucher(vid).redeemed.isoformat()
                if ds.voucher(vid).redeemed
                else None,
            }
            for vid in line.voucher_chain
        ]
        components.append(
            {
                "component": line.component,
                "weight": line.weight,
                "gross_cents": line.gross_cents,
                "issuer_share_cents": line.issuer_share_cents,
                "adjustment_cents": line.adjustment_cents,
                "refund_cents": line.refund_cents,
                "refund_receipts": list(line.refund_receipts),
                "refund_basis": line.refund_basis,
                "net_cents": line.net_cents,
                "state": line.state,
                "refund_policy": line.refundable,
                "sold_supplier": line.sold_supplier,
                "actual_supplier": line.supplier,
                "supplier_changed": line.supplier_changed,
                "voucher_chain": vouchers,
                "scheduled": line.scheduled.isoformat() if line.scheduled else None,
                "served": line.redeemed.isoformat() if line.redeemed else None,
                "service_period": line.service_period,
                "recognized_cents": line.recognized_cents,
                "deferred_cents": line.deferred_cents,
                "variable_cost_cents": line.variable_cost_cents,
                "contribution_cents": line.contribution_cents,
                "refund_at_risk_cents": line.refund_at_risk_cents,
                "effective_window_end": line.effective_window_end.isoformat(),
                "mature": line.mature,
            }
        )
    return {
        "order_id": order_id,
        "offer": order.offer,
        "household": order.household,
        "channel": order.channel,
        "purchase_date": order.purchase_date.isoformat(),
        "normal_window_end": order.normal_window_end.isoformat(),
        "effective_window_end": order_line.effective_window_end.isoformat(),
        "mature": order_line.mature,
        "capture_cents": order_line.capture_cents,
        "issuer_discount_cents": order_line.issuer_discount_cents,
        "platform_discount_cents": order_line.platform_discount_cents,
        "distributor_pool_cents": order_line.distributor_pool_cents,
        "net_cents": order_line.net_cents,
        "recognized_cents": order_line.recognized_cents,
        "deferred_cents": order_line.deferred_cents,
        "contribution_cents": order_line.contribution_cents,
        "refund_at_risk_cents": order_line.refund_at_risk_cents,
        "captures": [_money(m) for m in captures],
        "discounts": [_money(m) for m in discounts],
        "adjustments": [_money(m) for m in adjustments],
        "components": components,
        "refunds": [_refund(a) for a in order_line.refunds],
        "ledger": [_ledger(row) for row in ledger.lines_for_order(order_id)],
    }


def trace_claim(
    ds: Dataset,
    view: EconomicView,
    ledger: FinancialLedger,
    claim: str,
) -> Dict[str, object]:
    allocation = view.refund_for(claim)
    order = ds.order(allocation.order_id)
    order_line = view.order_line(allocation.order_id)
    reclasses = [
        line
        for line in ledger.lines_for_order(allocation.order_id)
        if line.source_kind == "dispute_reclass"
        and any(ref in line.reference for ref in allocation.refund_entry_ids)
    ]
    cash_lines = [
        line
        for line in ledger.lines_for_order(allocation.order_id)
        if line.source_kind in ("refund", "dispute_reclass")
        and any(
            ref in line.reference
            for ref in tuple(allocation.refund_entry_ids)
            + tuple(t.receipt for t in allocation.receipts)
        )
    ]
    affected = unit_economics(view, order.offer)
    return {
        "claim": claim,
        "order_id": allocation.order_id,
        "offer": order.offer,
        "channel": order.channel,
        "purchase_date": order.purchase_date.isoformat(),
        "refund": _refund(allocation),
        "service": [
            {
                "component": line.component,
                "state": line.state,
                "supplier": line.supplier,
                "sold_supplier": line.sold_supplier,
                "served": line.redeemed.isoformat() if line.redeemed else None,
                "service_period": line.service_period,
                "voucher_chain": list(line.voucher_chain),
            }
            for line in order_line.components
            if line.component != UNALLOCATED
        ],
        "ledger_lines": [_ledger(row) for row in cash_lines],
        "reclassed_in_periods": sorted({row.period for row in reclasses}),
        "still_in_suspense_cents": allocation.unallocated_cents,
        "offer_economics": affected.as_dict(),
        "decision_basis_changed": {
            "offer": order.offer,
            "deferred_cents": affected.deferred_cents,
            "refund_at_risk_cents": affected.refund_at_risk_cents,
            "note": (
                "the claim changes the offer's net contribution and refund "
                "exposure used by the keep/stop/replace comparison"
            ),
        },
    }
