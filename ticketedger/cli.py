"""Command line entry points.

Usage examples::

    python -m ticketedger summary --as-of 2026-11-23
    python -m ticketedger ledger --period 2026-11
    python -m ticketedger compare --as-of 2026-11-23
    python -m ticketedger trace-order O012
    python -m ticketedger trace-claim Q12
    python -m ticketedger reconcile
    python -m ticketedger report --out reports/operating_review.md

``--as-of`` defaults to the record horizon (today, if the records reach it), so
results are reproducible.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import List

from .allocation import build_economic_view
from .billing import bill_run_dict, build_bill_run, next_bill_period
from .financial import FinancialLedger
from .model import DataError, load_dataset
from .scenario import (
    compare_scenarios,
    decision_table,
    incurred_fixed,
    open_suspense_claims,
)
from .trace import trace_claim, trace_order


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_as_of(ds) -> date:
    """Decision date: today if the records reach it, else the record horizon.

    The dataset keeps future-dated bookings (orders and scheduled services).
    They are live obligations, so the default view keeps them but never counts
    an undelivered service as revenue.  ``--as-of`` moves the observation cut.
    """

    # Review the whole evidenced record by default.  ``--as-of`` narrows the
    # observation cut; delivery after that date is then treated as an
    # obligation rather than recognised revenue.
    return ds.horizon()


def _load(root: Path, as_of: str | None):
    ds = load_dataset(root)
    day = date.fromisoformat(as_of) if as_of else _default_as_of(ds)
    view = build_economic_view(ds, day)
    ledger = FinancialLedger(ds, view, day)
    return ds, view, ledger, day


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_summary(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    _print(
        {
            "as_of": day.isoformat(),
            "offers": [
                group.as_dict() for group in view.offer_summary()
            ],
            "mature_basis": {
                "observed": [
                    group.as_dict() for group in view.offer_summary(mature=True)
                ],
                "open_window": [
                    group.as_dict() for group in view.offer_summary(mature=False)
                ],
            },
            "unserved": [
                {
                    "component": line.component,
                    "state": line.state,
                    "offers": line.offer,
                    "deferred_cents": line.deferred_cents,
                    "refund_at_risk_cents": line.refund_at_risk_cents,
                }
                for line in view.outstanding()
            ],
            "suspense_cents": ledger.open_suspense(),
        }
    )


def cmd_ledger(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    periods = [args.period] if args.period else list(ledger.open_periods())
    payload = {}
    for period in periods:
        payload[period] = {
            "summary": next(
                (s.as_dict() for s in ledger.period_summary() if s.period == period),
                None,
            ),
            "lines": [
                {
                    "line_id": line.line_id,
                    "period": line.period,
                    "origin_period": line.origin_period,
                    "order_id": line.order_id,
                    "component": line.component,
                    "account": line.account,
                    "amount_cents": line.amount_cents,
                    "funder": line.funder,
                    "reference": line.reference,
                    "source_kind": line.source_kind,
                    "memo": line.memo,
                    "note": line.note,
                }
                for line in ledger.journal_for(period)
            ],
        }
    _print(payload)


def cmd_bill(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    period = args.period or next_bill_period(day, ledger)
    run = build_bill_run(ds, view, ledger, period)
    _print(bill_run_dict(run))


def cmd_compare(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    scenarios = decision_table(ds, view, day)
    keep, stop, replace = scenarios
    claims = open_suspense_claims(view)
    comparisons = [
        compare_scenarios(keep, stop, suspense_claims=claims).as_dict(),
        compare_scenarios(keep, replace, suspense_claims=claims).as_dict(),
        compare_scenarios(stop, replace, suspense_claims=claims).as_dict(),
    ]
    _print(
        {
            "as_of": day.isoformat(),
            "months": list(keep.months),
            "scenarios": [scenario.as_dict() for scenario in scenarios],
            "comparisons": comparisons,
            "incurred_fixed": [item.as_dict() for item in incurred_fixed(ds, view)],
        }
    )


def cmd_trace_order(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    _print(trace_order(ds, view, ledger, args.order_id))


def cmd_trace_claim(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    _print(trace_claim(ds, view, ledger, args.claim))


def cmd_reconcile(args) -> None:
    ds, view, ledger, day = _load(args.root, args.as_of)
    _print(
        {
            "as_of": day.isoformat(),
            "archive": [match.__dict__ for match in ledger.archive_reconciliation()],
            "suspense": [item.__dict__ for item in ledger.suspense_report()],
            "periods": [summary.as_dict() for summary in ledger.period_summary()],
        }
    )


def cmd_report(args) -> None:
    from .report import render_report

    ds, view, ledger, day = _load(args.root, args.as_of)
    text = render_report(ds, view, ledger, day)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root", type=Path, default=_default_root(), help="repository root"
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="decision date (default: latest date evidenced in the data)",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    parser = argparse.ArgumentParser(prog="ticketedger", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "summary", help="offer contribution and deferrals", parents=[common]
    )
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("ledger", help="period journal and summaries", parents=[common])
    p.add_argument("--period", default=None)
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("bill", help="next-period supplier bill run", parents=[common])
    p.add_argument("--period", default=None)
    p.set_defaults(func=cmd_bill)

    p = sub.add_parser(
        "compare", help="keep / stop / replace scenario table", parents=[common]
    )
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("trace-order", help="trace one ticket", parents=[common])
    p.add_argument("order_id")
    p.set_defaults(func=cmd_trace_order)

    p = sub.add_parser("trace-claim", help="trace one refund claim", parents=[common])
    p.add_argument("claim")
    p.set_defaults(func=cmd_trace_claim)

    p = sub.add_parser(
        "reconcile", help="archive, suspense and period check", parents=[common]
    )
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("report", help="render the operating review", parents=[common])
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyError as exc:
        missing = exc.args[0] if exc.args else exc
        print(f"未找到记录：{missing}", file=sys.stderr)
        return 2
    except DataError as exc:
        print(f"资料不一致：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
