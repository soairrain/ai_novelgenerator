"""3.4.4 张数换算与仓位上限。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from options_buyer.core.types import (
    CATEGORY_PREMIUM_MAX,
    CREDIT_MARGIN_MAX,
    DAY_LOSS_HARD_CAP,
    MULTIPLIER,
    SIGNAL_PREMIUM_MAX,
    SINGLE_PREMIUM_MAX,
    TOTAL_PREMIUM_MAX,
    AccountState,
    StrategyCategory,
    StrategyType,
)


@dataclass(frozen=True)
class SizeDecision:
    contracts: int
    premium_budget: float
    premium_cost: float
    ok: bool
    reason: str = ""


def floor_contracts(equity: float, premium_quote: float, pct: float = SINGLE_PREMIUM_MAX) -> int:
    """张数 = floor(账户 × 单笔权利金上限% / (权利金 × 100))."""
    if premium_quote <= 0 or equity <= 0:
        return 0
    budget = equity * pct
    per_contract = premium_quote * MULTIPLIER
    return math.floor(budget / per_contract)


def size_long_or_debit(
    equity: float,
    premium_quote: float,
    *,
    half_size: bool = False,
) -> SizeDecision:
    pct = SINGLE_PREMIUM_MAX * (0.5 if half_size else 1.0)
    n = floor_contracts(equity, premium_quote, pct)
    budget = equity * pct
    cost = n * premium_quote * MULTIPLIER
    if n < 1:
        return SizeDecision(0, budget, 0.0, False, "不足 1 张，放弃")
    return SizeDecision(n, budget, cost, True)


def credit_margin_ok(equity: float, margin: float) -> bool:
    return margin <= equity * CREDIT_MARGIN_MAX


def can_open_premium(
    account: AccountState,
    *,
    new_premium: float,
    category: StrategyCategory,
    strategy: StrategyType,
    margin: float = 0.0,
    max_loss: float | None = None,
) -> tuple[bool, str]:
    """开仓前仓位与日亏检查。

    - 裸买/借方：权利金预算（单笔 0.5% / 类别 2% / 合计 3%）
    - 信用价差：权利金预算不适用 max_loss；以保证金 ≤3% 为主；
      日亏检查用 max_loss（风险占用）与「日已实现净亏损 + 新仓风险 ≤2%」对齐
    """
    if account.halted:
        return False, f"已停机: {account.halt_reason}"

    day_loss = max(0.0, -account.day_realized_pnl)

    if strategy == StrategyType.CREDIT_SPREAD:
        # 文稿：保证金与权利金预算独立；信用实际约束通常是保证金。
        # 「日亏+新仓权利金≤2%」针对买方权利金；信用不把 max_loss 叠进该权利金口径。
        if day_loss >= account.equity * DAY_LOSS_HARD_CAP - 1e-9:
            return False, "日已实现净亏损已达 2%"
        if not credit_margin_ok(account.equity, margin):
            return False, "单笔信用价差保证金超过账户 3%"
        return True, "ok"

    if new_premium > account.equity * SINGLE_PREMIUM_MAX + 1e-9:
        return False, "单笔权利金超过账户 0.5%"

    cat_key = category.value
    cat_open = account.open_premium_by_category.get(cat_key, 0.0)
    if cat_open + new_premium > account.equity * CATEGORY_PREMIUM_MAX + 1e-9:
        return False, "单策略类别权利金超过账户 2%"

    if account.open_premium_total + new_premium > account.equity * TOTAL_PREMIUM_MAX + 1e-9:
        return False, "合计权利金超过账户 3%"

    # 日已实现净亏损 + 新仓权利金 ≤ 2%
    if day_loss + new_premium > account.equity * DAY_LOSS_HARD_CAP + 1e-9:
        return False, "日已实现净亏损 + 新仓权利金超过 2%"

    return True, "ok"


def cap_contracts_by_notional(
    equity: float,
    spot: float,
    contracts: int,
    *,
    max_multiple: float = 5.0,
) -> int:
    """名义敞口 > 账户 5 倍时降张数（3.4.4）。"""
    if contracts < 1 or spot <= 0 or equity <= 0:
        return 0
    while contracts >= 1 and contracts * MULTIPLIER * spot > equity * max_multiple:
        contracts -= 1
    return contracts


def signal_add_ok(
    account: AccountState,
    *,
    category: StrategyCategory,
    existing_premium: float,
    add_premium: float,
) -> tuple[bool, str]:
    """单信号加仓后总权利金 ≤ 1%。"""
    if existing_premium + add_premium > account.equity * SIGNAL_PREMIUM_MAX + 1e-9:
        return False, "单信号加仓后总权利金超过账户 1%"
    return can_open_premium(
        account,
        new_premium=add_premium,
        category=category,
        strategy=StrategyType.LONG,
    )
