"""Render the operating review as Markdown.

The report is generated, never hand-edited, so numbers always agree with the
code.  It answers the questions in the brief in order: contribution by offer
and cohort, the maturity split, refund evidence and suspense, the keep / stop /
replace comparison with its pending check, the next bill run, and one traced
claim.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Sequence

from .allocation import EconomicView
from .billing import bill_run_dict, build_bill_run, next_bill_period
from .financial import FinancialLedger
from .model import Dataset
from .scenario import (
    allocated_incurred_fixed,
    compare_scenarios,
    decision_table,
    incurred_fixed,
    open_suspense_claims,
    unit_economics,
)
from .trace import trace_claim


def money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def pct(part: int, whole: int) -> str:
    if whole == 0:
        return "n/a"
    return f"{part / whole * 100:.1f}%"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def render_report(
    ds: Dataset,
    view: EconomicView,
    ledger: FinancialLedger,
    as_of: date,
) -> str:
    out: List[str] = []
    add = out.append

    add("# 亲子研学联票：贡献、退款争议与关账复核")
    add("")
    add(
        f"- 决策日：{as_of.isoformat()}。只有该日及以前实际核销的子权益确认收入；"
        "该日之后才接待的部分（若数据已排期）作为待履约，不计入当期贡献；"
        "观察期与“待定退款风险”也按该日判断。"
    )
    add(f"- 数据中可佐证的最晚日期：{ds.horizon().isoformat()}")
    add("- 金额单位：人民币分（报告表格换算为元）")
    add(
        "- 两个口径：**经营口径**按购买批次与接待月份归集；"
        "**财务口径**按凭证入账月份归集，已关账月份不重开。"
    )
    add("")

    # 1. Offer level -----------------------------------------------------
    add("## 1. 组合层面的净贡献（经营口径）")
    add("")
    add("实收为整单收款；净额已扣发行方整单红包、指定子权益减免与已归属退款。"
        "平台补贴为备忘项，不重复冲减。"
        "“履约贡献”只含已实际核销的子权益；未核销部分是待履约义务。")
    add("")
    rows = []
    for group in view.offer_summary():
        rows.append(
            [
                group.key[0],
                group.orders,
                money(group.capture_cents),
                money(group.net_cents),
                money(group.recognized_cents),
                money(group.variable_cost_cents),
                money(group.contribution_cents),
                money(group.deferred_cents),
                money(group.refund_at_risk_cents),
            ]
        )
    add(
        _table(
            [
                "组合",
                "订单",
                "实收",
                "净额",
                "已确认收入",
                "随单成本",
                "履约贡献",
                "待履约",
                "待定退款风险",
            ],
            rows,
        )
    )
    add("")

    add("### 1.1 已发生固定款的分摊")
    add("")
    add("共享一项工作坊的组合按当月实际交付份数分摊该供应商承诺；"
        "分摊后的净贡献才是组合真实留下的金额。")
    add("")
    fixed_alloc, fixed_sources = allocated_incurred_fixed(ds, view)
    rows = []
    for group in view.offer_summary():
        offer = group.key[0]
        fixed = fixed_alloc.get(offer, 0)
        rows.append(
            [
                offer,
                money(group.contribution_cents),
                money(fixed),
                money(group.contribution_cents - fixed),
                "、".join(fixed_sources.get(offer, ())) or "—",
            ]
        )
    add(
        _table(
            ["组合", "履约贡献", "分摊固定款", "净贡献", "固定款来源"],
            rows,
        )
    )
    add("")
    add("> 单看履约贡献会把固定承诺漏掉。即使组合已走满，"
        "共享工作坊与接驳的不可避免承诺仍要按实际交付分摊进来；"
        "尚未产生交付的组合没有可分摊的固定款，但仍占用对应产能。")
    add("")

    # 2. Cohort maturity -------------------------------------------------
    add("## 2. 销售额高但观察期未走满的批次要分开看")
    add("")
    add("观察期 = 订单通常期限，若存在延期争议则取单据最迟处理日。"
        "决策日仍未走满观察期的订单，其退款风险与已走满批次不可直接相加比较。")
    add("")
    rows = []
    for offer in [g.key[0] for g in view.offer_summary()]:
        mature = unit_economics(view, offer, mature=True)
        whole = unit_economics(view, offer)
        open_part = unit_economics(view, offer, mature=False)
        rows.append(
            [
                offer,
                whole.orders,
                mature.orders,
                money(mature.contribution_cents),
                money(mature.deferred_cents),
                open_part.orders,
                money(open_part.contribution_cents),
                money(open_part.deferred_cents),
                money(open_part.refund_at_risk_cents),
            ]
        )
    add(
        _table(
            [
                "组合",
                "总订单",
                "已走满单数",
                "已走满履约贡献",
                "已走满待履约",
                "未走满单数",
                "未走满已确认",
                "未走满待履约",
                "未走满退款风险",
            ],
            rows,
        )
    )
    add("")
    add("> 未走满批次的“已确认收入”只代表已核销部分，不能当作该批次最终贡献；"
        "同一组合的整体贡献必须等观察期走满再定论。")
    add("")

    # 3. Batch / channel -------------------------------------------------
    add("## 3. 按购买批次、接待月份与渠道")
    add("")
    add("### 3.1 购买批次 × 渠道")
    add("")
    rows = []
    for group in view.batch_summary():
        rows.append(
            [
                group.key[0],
                group.key[1],
                group.key[2],
                group.orders,
                money(group.net_cents),
                money(group.recognized_cents),
                money(group.contribution_cents),
                money(group.deferred_cents),
                money(group.refund_at_risk_cents),
            ]
        )
    add(
        _table(
            [
                "购买月份",
                "组合",
                "渠道",
                "订单",
                "净额",
                "已确认",
                "履约贡献",
                "待履约",
                "退款风险",
            ],
            rows,
        )
    )
    add("")
    add("### 3.2 接待月份 × 组合")
    add("")
    add("未履行的子权益单列为 `unserved`，避免只看已接待月份而漏掉待履约与可能退款。")
    add("")
    rows = []
    for group in view.month_summary():
        rows.append(
            [
                group.key[0],
                group.key[1],
                group.orders,
                money(group.recognized_cents),
                money(group.variable_cost_cents),
                money(group.contribution_cents),
                money(group.deferred_cents),
            ]
        )
    add(
        _table(
            ["接待月份", "组合", "订单", "已确认收入", "随单成本", "贡献", "未履行"],
            rows,
        )
    )
    add("")

    # 4. Fixed cost ------------------------------------------------------
    add("## 4. 固定承诺：已发生与可避免性")
    add("")
    incurred = incurred_fixed(ds, view)
    add("已发生固定款按供应商×月份归集；只有合同注明可避免且没有其它在售组合继续使用时，"
        "停售情景才可移除。共享同一工作坊的组合会同时承担该项承诺。")
    add("")
    rows = [
        [item.term_id, item.supplier, item.component, item.month, money(item.cents), "是" if item.avoidable else "否"]
        for item in incurred
    ]
    add(_table(["合同", "供应方", "子权益", "月份", "固定款", "可避免"], rows))
    add("")
    totals: Dict[str, int] = {}
    for item in incurred:
        totals[item.term_id] = totals.get(item.term_id, 0) + item.cents
    add(
        "合计："
        + "；".join(f"{term} {money(cents)} 元" for term, cents in sorted(totals.items()))
        + f"；总计 {money(sum(totals.values()))} 元。"
    )
    add("")

    # 5. Refund evidence -------------------------------------------------
    add("## 5. 退款认领：替换回执、未分配与待认领")
    add("")
    add("退款现金按最终回执认领；`replacement_of` 指向旧回执的，只计最终版本。"
        "回执未覆盖的部分留在待认领，不记到最近核销方。")
    add("")
    rows = []
    for allocation in view.refunds():
        final = [r for r in allocation.receipts if r.is_final]
        rows.append(
            [
                allocation.claim,
                allocation.order_id,
                ", ".join(allocation.refund_entry_ids),
                money(allocation.cash_cents),
                ", ".join(
                    f"{component}:{money(item.cents)}（{item.basis}）"
                    for component, item in sorted(allocation.components.items())
                )
                or "—",
                money(allocation.unallocated_cents),
                ", ".join(r.receipt for r in final) or "无最终回执",
                allocation.final_resolution.isoformat()
                if allocation.final_resolution
                else "—",
                allocation.refund_period or "—",
            ]
        )
    add(
        _table(
            [
                "claim",
                "订单",
                "退款凭证",
                "退款现金",
                "已认领",
                "待认领",
                "最终回执",
                "最终处理日",
                "现金入账月",
            ],
            rows,
        )
    )
    add("")
    for allocation in view.refunds():
        if allocation.unallocated_cents:
            add(
                f"> **{allocation.claim}** 仍有 {money(allocation.unallocated_cents)} 元"
                f"待认领：{allocation.notes[-1] if allocation.notes else '缺凭证'}。"
                "该金额未计入任何子权益，也未推给最近核销方。"
            )
            add("")
    note_claims = [a for a in view.refunds() if any("superseded" in n for n in a.notes)]
    if note_claims:
        add("替换回执处理：")
        for allocation in note_claims:
            superseded = [
                r for r in allocation.receipts if not r.is_final
            ]
            for receipt in superseded:
                add(
                    f"- {allocation.claim}: 回执 {receipt.receipt}"
                    f"（{money(receipt.resolved_cents)} 元，{receipt.result}）"
                    f"已被 {'、'.join(receipt.superseded_by)} 替换，只按最终版本计一次。"
                )
        add("")

    # 6. Scenarios -------------------------------------------------------
    add("## 6. 现售、停售与替代组合的经营比较")
    add("")
    scenarios = decision_table(ds, view, as_of)
    keep, stop, replace = scenarios
    add(f"情景窗口：{', '.join(keep.months)}（连续 {len(keep.months)} 个月）。"
        "需求率只取决策日之前已售订单，按已售月份折算；"
        "票面贡献仍取该组合全部订单的单价与随单成本。"
        "替代组合假设需求一对一转入，属于情景假设而非观测数据。")
    add("")
    rows = []
    for scenario in scenarios:
        rows.append(
            [
                scenario.name,
                money(scenario.expected_contribution_cents),
                money(scenario.capacity_cost_cents),
                money(scenario.net_monthly_cents),
                money(scenario.carried_deferred_cents),
                money(scenario.carried_refund_risk_cents),
                money(scenario.cost_per_ticket_cents) if scenario.units_per_month else "—",
            ]
        )
    add(
        _table(
            [
                "情景",
                "月均预期贡献",
                "月承诺固定款",
                "月净贡献",
                "停售承接的待履约",
                "停售承接的退款敞口",
                "每票承担承诺（首月口径）",
            ],
            rows,
        )
    )
    add("")
    add("“停售承接的退款敞口”按可退但尚未赚到的金额计：观察期走满只结束"
        "窗口内争议，不会把未核销的可退权益变成确定收入。")
    add("")
    add("### 6.1 停售后真正消失的支出")
    add("")
    for scenario in scenarios:
        avoided = [item for item in scenario.fixed_detail if item.status == "avoided"]
        if not avoided:
            continue
        names = sorted({f"{item.term_id}/{item.supplier}{item.component}" for item in avoided})
        add(f"- {scenario.name} 可移除：{', '.join(names)}。")
    add("")
    add("不可避免或仍被在售组合共享的合同（如供应商 B 的工作坊、供应商 C 的接驳）"
        "即使停售也继续计入，因此停售并非“销售额归零、成本也归零”。")
    add("")
    claims = open_suspense_claims(view)
    add("### 6.2 比较是否会被未认领金额推翻")
    add("")
    comparisons = [
        compare_scenarios(keep, stop, suspense_claims=claims),
        compare_scenarios(keep, replace, suspense_claims=claims),
        compare_scenarios(stop, replace, suspense_claims=claims),
    ]
    rows = [
        [
            c.left,
            c.right,
            money(c.delta_monthly_cents),
            money(c.avoidable_fixed_saved_cents),
            "待定" if c.pending else "可定",
            ", ".join(c.missing_evidence) or "—",
            (
                f"待认领 {money(c.suspense_cents)} 元不小于月差额，"
                "认领结果可能翻转排名"
                if c.pending
                else (
                    f"待认领 {money(c.suspense_cents)} 元低于月差额，排名稳定但需披露"
                    if c.suspense_cents
                    else "退款现金均已归属，排名可定"
                )
            ),
        ]
        for c in comparisons
    ]
    add(
        _table(
            ["左情景", "右情景", "月差额", "可省固定款", "结论", "缺证据", "说明"],
            rows,
        )
    )
    add("")
    open_claims = [a for a in view.refunds() if a.unallocated_cents]
    if any(c.pending for c in comparisons):
        add("> 存在未认领金额不小于情景差额，比较列为**待定**，需先取得对应退款回执。")
    elif open_claims:
        add("> 现有待认领金额低于各情景差额，排名暂不因未认领现金翻转；"
            "但该金额仍需单独披露，不能静默并入任一组合，认领后要重跑比较。")
    else:
        add("> 现有退款现金均已按最终回执或渠道明示归属认领，没有未认领余额；"
            "若后续出现缺凭证的退款，比较需重新判断是否列为待定。")
    add("")

    # 7. Ledger ----------------------------------------------------------
    add("## 7. 财务口径：期间、关账与后续争议")
    add("")
    rows = []
    for summary in ledger.period_summary():
        rows.append(
            [
                summary.period,
                {"closed": "已关账", "open": "开放"}.get(summary.state, summary.state),
                money(summary.cash_capture_cents),
                money(summary.issuer_discount_cents),
                money(summary.adjustment_cents),
                money(summary.refund_cents),
                money(summary.suspense_delta_cents),
                money(summary.cash_basis_net_cents),
                money(summary.revenue_cents),
                money(summary.variable_cost_cents),
                money(summary.sealed_archive_cents),
            ]
        )
    add(
        _table(
            [
                "月份",
                "状态",
                "收款",
                "发行方红包",
                "调价",
                "退款",
                "待认领变动",
                "现金口径净额",
                "确认收入",
                "随单成本",
                "关账归档",
            ],
            rows,
        )
    )
    add("")
    add("已关账月份只保留归档凭证与当时金额；后续争议结论记入下一开放月，"
        "并保留原销售期与退款期。平台补贴仅备忘，不进入净额。"
        "开放月的“确认收入/随单成本”只列该月实际接待；"
        "已关账月份的履约不在此重复推导，其原始发生额以归档为准。")
    add("")
    rows = []
    for match in ledger.archive_reconciliation():
        rows.append(
            [
                match.book_id,
                match.period,
                match.source,
                match.entry_scope,
                money(match.posted_cents),
                money(match.expected_cents) if match.expected_cents is not None else "—",
                money(match.difference_cents) if match.difference_cents is not None else "—",
                "一致" if match.matched else "不一致",
            ]
        )
    add(_table(["归档", "月份", "来源", "口径", "归档金额", "源凭证推导", "差异", "核对"], rows))
    add("")
    add("### 7.1 待认领余额")
    add("")
    rows = [
        [
            item.claim,
            item.order_id,
            item.offer,
            money(item.amount_cents),
            money(item.resolved_cents),
            item.status,
            item.booked_period,
            item.released_period or "—",
            "、".join(item.evidence) or "—",
        ]
        for item in ledger.suspense_report()
    ]
    add(
        _table(
            ["claim", "订单", "组合", "待认领", "已认领", "状态", "入账月", "释放月", "最终回执"],
            rows,
        )
    )
    add("")

    # 8. Next bills ------------------------------------------------------
    add("## 8. 下一期供应商账单")
    add("")
    period = next_bill_period(as_of, ledger)
    run = build_bill_run(ds, view, ledger, period)
    payload = bill_run_dict(run)
    add(f"账期 **{period}**（截至 {run.cut_off.isoformat()} 的已排期券，非需求预测）。")
    add("")
    rows = [
        [
            line["supplier"],
            line["component"],
            "按次" if line["basis"] == "delivery" else "固定",
            line["units"],
            money(line["unit_cents"]),
            money(line["amount_cents"]),
            ", ".join(line["offers"]) or "—",
            line["reference"],
        ]
        for line in payload["lines"]
    ]
    add(
        _table(
            ["供应方", "子权益", "计费", "数量/份", "单价", "金额", "关联组合", "凭据"],
            rows,
        )
    )
    add("")
    add(
        f"合计 {money(payload['total_cents'])} 元：按次 {money(payload['variable_cents'])}，"
        f"固定 {money(payload['fixed_cents'])}。"
    )
    add("")

    # 9. Trace -----------------------------------------------------------
    add("## 9. 沿一张争议联票追查")
    add("")
    claim = next(
        (a.claim for a in view.refunds() if a.unallocated_cents),
        view.refunds()[0].claim if view.refunds() else None,
    )
    if claim:
        trace = trace_claim(ds, view, ledger, claim)
        add(f"以 claim **{claim}**（订单 {trace['order_id']}，组合 {trace['offer']}）为例：")
        add("")
        rows = []
        for item in trace["service"]:
            rows.append(
                [
                    item["component"],
                    item["state"],
                    item["sold_supplier"],
                    item["supplier"],
                    "→".join(item["voucher_chain"]),
                    item["served"] or "—",
                ]
            )
        add(_table(["子权益", "状态", "售出版本供应方", "实际供应方", "券沿革", "核销日"], rows))
        add("")
        add("退款与账务：")
        add("")
        rows = []
        for line in trace["ledger_lines"]:
            rows.append(
                [
                    line["line_id"],
                    line["period"],
                    line["origin_period"],
                    line["account"],
                    line["component"],
                    money(line["amount_cents"]),
                    line["closed"],
                    line["note"],
                ]
            )
        add(
            _table(
                ["凭证", "入账月", "原期", "科目", "子权益", "金额", "已关账", "说明"],
                rows,
            )
        )
        add("")
        refund = trace["refund"]
        add(
            f"- 退款现金 {money(refund['cash_cents'])} 元，入账月 {refund['refund_period']}；"
            f"最终处理日 {refund['final_resolution']}。"
        )
        add(
            f"- 已认领 {money(refund['cash_cents'] - refund['unallocated_cents'])} 元；"
            f"待认领 {money(refund['unallocated_cents'])} 元。"
        )
        for receipt in refund["receipts"]:
            status = "最终" if receipt["final"] else (
                "已被 " + "、".join(receipt["superseded_by"]) + " 替换"
            )
            add(
                f"- 回执 {receipt['receipt']}（{receipt['component']}，"
                f"{money(receipt['resolved_cents'])} 元，{receipt['result']}，{status}）。"
            )
        if trace["reclassed_in_periods"]:
            add(
                f"- 后续争议金额记入开放期 {', '.join(trace['reclassed_in_periods'])}，"
                "并反查原始销售期；原关账金额未重开。"
            )
        add(
            f"- 该组合当前待履约 {money(trace['decision_basis_changed']['deferred_cents'])} 元，"
            f"退款风险 {money(trace['decision_basis_changed']['refund_at_risk_cents'])} 元；"
            "争议结论已改变其处置依据。"

        )
        add("")

    # 10. Findings -------------------------------------------------------
    add("## 10. 结论与待办")
    add("")
    immature = [
        f"{group.key[0]}（{sum(1 for o in view.orders if o.offer == group.key[0] and not o.mature)} 单）"
        for group in view.offer_summary()
        if any(o.offer == group.key[0] and not o.mature for o in view.orders)
    ]
    if immature:
        add("1. 不能只看销售额：仍有订单未走满观察期——"
            + "、".join(immature)
            + "；这些批次须与已走满批次分开比较。")
    else:
        add("1. 决策日所有订单均已走满观察期，批次间可直接比较；"
            "但未核销子权益仍是待履约义务。")
    add("2. 停售要按“真正消失的支出”比较：不可避免的 B、C 合同仍计入，"
        "共享工作坊不能重复节省。")
    add("3. 老票仍按售出版本兑现；换券改变供应方时，收入与成本归实际履约方。")
    channel_only = [
        a.claim
        for a in view.refunds()
        if a.components and all(item.basis == "channel" for item in a.components.values())
    ]
    if open_claims:
        add(
            "4. 仍有待认领退款："
            + "、".join(f"{a.claim} {money(a.unallocated_cents)} 元" for a in open_claims)
            + "；不得推给最近核销方，相关比较若被推翻应列为待定。"
        )
    else:
        add("4. 现有退款现金均已按最终回执或渠道明示归属认领；"
            "未明示归属的金额才会留在待认领，不得推给最近核销方。")
    if channel_only:
        add(
            "   - " + "、".join(channel_only) + " 没有争议回执，"
            "仅凭退款凭证自带的 component 认领，证据强度低于回执。"
        )
    add("5. 已关账月份不重开；争议金额进下一开放期并反查原批次。")
    add("")
    return "\n".join(out)
