"""Family combo-ticket contribution, refund-dispute and close ledger.

The package keeps two deliberately different views of the same synthetic
business records:

* an operating view (``EconomicView``) keyed by the order / purchase cohort
  and by the month service was actually delivered, and
* a financial view (``FinancialLedger``) keyed by the period in which
  cash/credits were booked, honouring sealed books.

All amounts are integer RMB cents.  Source records live in ``data/`` and are
never modified.
"""

from .model import Dataset, DataError, load_dataset
from .allocation import (
    ComponentLine,
    EconomicView,
    GroupMetrics,
    OrderLine,
    build_economic_view,
)
from .financial import FinancialLedger, LedgerLine, PeriodSummary, SuspenseItem
from .refunds import RefundAllocation, ReceiptTrace, resolve_refunds

__all__ = [
    "Dataset",
    "DataError",
    "load_dataset",
    "EconomicView",
    "FinancialLedger",
    "build_economic_view",
    "resolve_refunds",
    "ComponentLine",
    "OrderLine",
    "GroupMetrics",
    "LedgerLine",
    "PeriodSummary",
    "SuspenseItem",
    "RefundAllocation",
    "ReceiptTrace",
]
