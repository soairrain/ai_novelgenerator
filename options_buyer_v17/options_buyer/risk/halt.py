"""止损计数状态机与日亏硬上限（v1.7）。

- 连续 3 笔止损 → 当日停机（按交易笔数，不按 R）
- 盈利平仓 → 连续计数归 0
- 加仓后一笔亏 2R 仍算 1 笔
- 日已实现净亏损达 2% → 停机，计数不再重置
"""

from __future__ import annotations

from options_buyer.core.types import (
    CONSECUTIVE_STOP_HALT,
    DAY_LOSS_HARD_CAP,
    AccountState,
)


def record_stop(account: AccountState, *, is_add_leg: bool = False) -> AccountState:
    """止损平仓：+1 笔（加仓亏损仍只 +1，调用方对整笔平仓调用一次）。"""
    if account.halted and account.halt_reason and "日亏" in account.halt_reason:
        return account  # 日亏停机后不再重置计数

    account.consecutive_stops += 1
    if account.consecutive_stops >= CONSECUTIVE_STOP_HALT:
        account.halted = True
        account.halt_reason = f"连续 {CONSECUTIVE_STOP_HALT} 笔止损"
    return account


def record_win(account: AccountState) -> AccountState:
    """盈利平仓 → 连续止损计数归 0。"""
    if account.halted and account.halt_reason and "日亏" in (account.halt_reason or ""):
        return account
    account.consecutive_stops = 0
    account.wins_today += 1
    account.consecutive_wins += 1
    return account


def record_flat_or_loss_non_stop(account: AccountState) -> AccountState:
    """非止损、非盈利（如时间止损微亏）不重置也不 +1。"""
    account.consecutive_wins = 0
    return account


def apply_realized_pnl(account: AccountState, pnl: float) -> AccountState:
    account.day_realized_pnl += pnl
    day_loss = max(0.0, -account.day_realized_pnl)
    if day_loss >= account.equity * DAY_LOSS_HARD_CAP - 1e-9:
        account.halted = True
        account.halt_reason = "日已实现净亏损达 2%（硬上限）"
    if pnl <= 0:
        account.consecutive_wins = 0
    return account


def reset_day(account: AccountState, *, equity: float | None = None) -> AccountState:
    """新交易日重置（日亏停机在次日解除）。"""
    if equity is not None:
        account.equity = equity
    account.day_realized_pnl = 0.0
    account.consecutive_stops = 0
    account.halted = False
    account.halt_reason = None
    account.opens_today = 0
    account.wins_today = 0
    account.consecutive_wins = 0
    account.extreme_pause_until = None
    return account


def size_multiplier_psychology(account: AccountState) -> float:
    """连续 2 笔盈利后下一笔仓位减半（软规则）。"""
    if account.consecutive_wins >= 2:
        return 0.5
    return 1.0


def soft_overtrade_stop(account: AccountState) -> bool:
    """当日已开仓 ≥5 笔且无盈利 → 建议停手（软规则）。"""
    return account.opens_today >= 5 and account.wins_today == 0
