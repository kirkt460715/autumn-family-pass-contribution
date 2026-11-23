# 亲子研学联票的贡献核算与退款争议

场馆联盟的家庭票服务与往来账。财务凭证、核销资料和成本合同均为独立合成记录。 `data/` 中的记录均为虚构业务数据。

使用 Python 3.11.2，仅依赖标准库。源码在 `ticketedger/`，测试在 `tests/`。

```bash
python3 -m unittest discover -s tests -v
```

## 两套口径

同一批联票同时保留两种看法，互不覆盖：

- **经营口径**（`ticketedger/allocation.py`）：按购买批次、组合版本、接待月份和渠道归集。
  收入只在子权益实际核销时确认；未核销和决策日后才接待的部分是待履约义务。
- **财务口径**（`ticketedger/financial.py`）：按凭证入账月份归集，尊重已关账月份。
  已关账月份只保留归档金额；后续争议结论记入下一开放月，并保留原销售期与退款期。

## 关键规则

| 主题 | 处理 |
|---|---|
| 整单红包 | 发行方出资的减少可分金额；平台出资的只作备忘，不重复冲减 |
| 分摊 | 按售出版本公示权重的最大余数法拆分到分，子项之和恒等于整单 |
| 换券 | 沿 `previous` 追溯；被替换或 `void` 的旧券不确认收入，新券继承权利，可换日期、供应方或家庭成员 |
| 服务收入 | 只有决策日前实际核销的活券才确认；实际供应方承担成本 |
| 退款认领 | 只认最终回执（`replacement_of` 指向的旧回执不重复计）或退款凭证自带的 component；其余留在待认领，不推给最近核销方 |
| 观察期 | 通常期限，遇延期争议取单据最迟处理日；未走满批次与已走满批次分开比较 |
| 停售 | 老票仍按售出版本兑现；只移除合同注明可避免且无在售组合继续使用的固定款 |
| 比较待定 | 待认领金额不小于情景差额时，比较列为待定并列出缺哪笔证据 |
| 关账 | 不重开；后续争议进下一开放期并反查原批次 |

## 命令

```bash
python3 -m ticketedger summary                 # 组合贡献、待履约、退款风险（含成熟度分组）
python3 -m ticketedger ledger --period 2026-11 # 开放期日记账
python3 -m ticketedger compare                 # 现售 / 停售 / 替代比较
python3 -m ticketedger bill                    # 下一期供应商账单
python3 -m ticketedger reconcile               # 归档、待认领与期间核对
python3 -m ticketedger trace-order O012        # 沿一张联票追查
python3 -m ticketedger trace-claim Q12         # 沿一笔争议追查
python3 -m ticketedger report --out reports/operating_review.md
```

默认决策日取数据可佐证的最晚日期；用 `--as-of YYYY-MM-DD` 可把观察截止日提前
（此前未核销、未收到回执的部分会转为待履约或待认领）。

已生成的经营复核见 [reports/operating_review.md](reports/operating_review.md)。

各表来源及字段约定见 [资料约定](data-contract.md)。
