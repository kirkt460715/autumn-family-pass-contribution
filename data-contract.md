# 资料约定

CSV 采用 UTF-8 编码。日期使用年、月、日，数值以十进制表示。字段为空表示来源未提供该项或该项不适用，含义以表约定为准。

金额均为人民币分，入账期以 `books` 登记为准，成本区分随单成本和仍承担的期间固定款。规则按售出版本选用，票价输入含各子项公示分摊权重。平台优惠表中的资金方决定该部分是否减少联票可分金额；金额为正的收款和退款凭证分别列出。退款回执按 claim 关联到退款原凭证，明确写有 replacement_of 时只是替换书面回执。
子券通过 `previous` 追溯原始权利。转签不改变原票版本，`void` 表示旧券停止流通，不消灭转签的新券。退款与赔款凭证中的 component 可留空，留空表示收款渠道尚未确认归属。已关闭账期提供的是独立归档凭证与金额，后续新增凭据须保留可追溯的原销售期。
售票后十四日为通常的观察期限，个别延期争议以单据上的最迟处理日为准。停售后的履约成本按原票条款继续记入，固定成本只有合同注明可避免才可以从新组合中移出。

## 文件来源

- `offers.csv`。销售版本下的子权益。权重为正整数，退款权限为 open、unused 或 none。 记录键为 `offer`、`component`。
- `orders.csv`。下单批次及约定通常期限。 记录键为 `order_id`。
- `vouchers.csv`。单项核销和换券沿革。guest 只在当前订单内有意义。 记录键为 `coupon_id`。
- `money.csv`。渠道收退款、费用承担者、调整与更正事件。 记录键为 `entry_id`。
- `disputes.csv`。争议分项回执，partial 为该回执只确认其所列金额，closed 为最终分项凭证。 记录键为 `receipt`。
- `books.csv`。账期状态按自然月生效。 记录键为 `period`。
- `book_entries.csv`。已签章且相关的关账行摘录。非全量明细，原入账金额含冲减。 记录键为 `book_id`。
- `supplier_terms.csv`。月度固定承诺及可避免条件。 记录键为 `term_id`。
- `commercial_actions.csv`。经营组提出和确认的产品状态。 记录键为 `action_id`。

## 由本仓库代码补充的口径

以下口径不改变源表，只在 `ticketedger/` 中解释和归集：

- **可分金额**：整单收款减去发行方出资的整单红包。平台出资的整单红包只作备忘。
- **子项分摊**：按 `offers.weight` 用最大余数法拆分，分到分，子项之和等于可分金额。
- **退款认领顺序**：先取同一 `claim` 下未被替换的最终回执；没有回执时，只认 `money.component` 非空的部分；两者都没有则留在待认领。
- **待认领**：单列为 `unallocated`，不进入任何子项、不确认收入、不推给最近核销方。
- **观察期**：`orders.normal_window_end`；若该订单有争议，取 `disputes.latest_resolution` 的较晚者。
- **固定款**：`supplier_terms.fixed_cents` 按供应商×子权益×月份；只有 `avoidable_if_stopped=yes` 且无在售组合继续使用该供应方/子项时才可在停售情景移除。`cancelled_on` 之后月份不再计费。
- **关账**：`books.state=closed` 的月份只保留 `book_entries` 归档；后续争议记入下一开放期，`origin_period` 保留原退款期。
