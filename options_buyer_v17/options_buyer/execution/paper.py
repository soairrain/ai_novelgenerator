"""纸面账户：开平仓走完整风控路径。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from options_buyer.core.exits import (
    ExitAction,
    ExitDecision,
    credit_spread_exit,
    iv_path_override,
    long_or_debit_exit,
    pnl_in_r,
)
from options_buyer.core.r import compute_r, structure_target_ok
from options_buyer.core.sizing import (
    can_open_premium,
    cap_contracts_by_notional,
    size_long_or_debit,
)
from options_buyer.core.types import (
    MULTIPLIER,
    AccountState,
    ExitReason,
    Position,
    StrategyCategory,
    StrategyType,
)
from options_buyer.risk import halt as halt_mod


@dataclass
class Fill:
    position_id: str
    side: str
    contracts: int
    price: float
    pnl: float = 0.0
    reason: str = ""


@dataclass
class PaperBroker:
    account: AccountState
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    def _reserve_premium(self, pos: Position, amount: float) -> None:
        """买方/借方：占用权利金预算。"""
        pos.premium_reserved = amount
        self.account.open_premium_total += amount
        cat = pos.category.value
        self.account.open_premium_by_category[cat] = (
            self.account.open_premium_by_category.get(cat, 0.0) + amount
        )

    def _release_premium(self, pos: Position, fraction: float = 1.0) -> float:
        amount = pos.premium_reserved * fraction
        self.account.open_premium_total = max(0.0, self.account.open_premium_total - amount)
        cat = pos.category.value
        self.account.open_premium_by_category[cat] = max(
            0.0, self.account.open_premium_by_category.get(cat, 0.0) - amount
        )
        pos.premium_reserved = max(0.0, pos.premium_reserved - amount)
        return amount

    def _reserve_credit_margin(self, pos: Position, margin: float) -> None:
        """信用价差：只占用保证金，不计入权利金预算（§4.3）。"""
        pos.margin = margin
        pos.premium_reserved = 0.0
        self.account.open_credit_margin_total += margin

    def _release_credit_margin(self, pos: Position) -> float:
        m = pos.margin
        self.account.open_credit_margin_total = max(
            0.0, self.account.open_credit_margin_total - m
        )
        pos.margin = 0.0
        return m

    def open_long(
        self,
        *,
        symbol: str,
        premium: float,
        category: StrategyCategory,
        dte: int,
        structure_target_r: float,
        half_size: bool = False,
        is_add: bool = False,
        spot: float | None = None,
    ) -> Position | None:
        psych = halt_mod.size_multiplier_psychology(self.account) < 1.0
        decision = size_long_or_debit(
            self.account.equity, premium, half_size=half_size or psych
        )
        if not decision.ok:
            return None
        contracts = decision.contracts
        if spot is not None:
            contracts = cap_contracts_by_notional(self.account.equity, spot, contracts)
            if contracts < 1:
                return None
        premium_cost = contracts * premium * MULTIPLIER

        ok, _ = can_open_premium(
            self.account,
            new_premium=premium_cost,
            category=category,
            strategy=StrategyType.LONG,
        )
        if not ok:
            return None
        if not structure_target_ok(structure_target_r, StrategyType.LONG):
            return None

        rspec = compute_r(StrategyType.LONG, premium=premium, contracts=contracts)
        pid = str(uuid.uuid4())[:8]
        pos = Position(
            position_id=pid,
            strategy=StrategyType.LONG,
            category=category,
            symbol=symbol,
            contracts=contracts,
            entry_premium=premium,
            r_dollars=rspec.r_dollars,
            r_per_contract=rspec.r_per_contract,
            dte_at_entry=dte,
            structure_target_r=structure_target_r,
            is_add=is_add,
            added_once=is_add,
        )
        self._reserve_premium(pos, premium_cost)
        self.positions[pid] = pos
        self.account.opens_today += 1
        self.fills.append(Fill(pid, "buy", contracts, premium))
        return pos

    def open_debit(
        self,
        *,
        symbol: str,
        net_debit: float,
        category: StrategyCategory,
        dte: int,
        structure_target_r: float,
        width: float = 0.0,
        half_size: bool = False,
        spot: float | None = None,
    ) -> Position | None:
        psych = halt_mod.size_multiplier_psychology(self.account) < 1.0
        decision = size_long_or_debit(
            self.account.equity, net_debit, half_size=half_size or psych
        )
        if not decision.ok:
            return None
        contracts = decision.contracts
        if spot is not None:
            contracts = cap_contracts_by_notional(self.account.equity, spot, contracts)
            if contracts < 1:
                return None
        premium_cost = contracts * net_debit * MULTIPLIER

        ok, _ = can_open_premium(
            self.account,
            new_premium=premium_cost,
            category=category,
            strategy=StrategyType.DEBIT_SPREAD,
        )
        if not ok:
            return None
        if not structure_target_ok(structure_target_r, StrategyType.DEBIT_SPREAD):
            return None

        rspec = compute_r(
            StrategyType.DEBIT_SPREAD, debit=net_debit, contracts=contracts
        )
        pid = str(uuid.uuid4())[:8]
        pos = Position(
            position_id=pid,
            strategy=StrategyType.DEBIT_SPREAD,
            category=category,
            symbol=symbol,
            contracts=contracts,
            entry_premium=net_debit,
            width=width,
            r_dollars=rspec.r_dollars,
            r_per_contract=rspec.r_per_contract,
            dte_at_entry=dte,
            structure_target_r=structure_target_r,
        )
        self._reserve_premium(pos, premium_cost)
        self.positions[pid] = pos
        self.account.opens_today += 1
        self.fills.append(Fill(pid, "buy_debit", contracts, net_debit))
        return pos

    def open_credit(
        self,
        *,
        symbol: str,
        width: float,
        credit: float,
        margin: float,
        category: StrategyCategory,
        dte: int,
        structure_target_r: float,
        contracts: int = 1,
    ) -> Position | None:
        max_loss = (width - credit) * MULTIPLIER * contracts
        ok, _ = can_open_premium(
            self.account,
            new_premium=0.0,  # 信用不以权利金 0.5% 约束
            category=category,
            strategy=StrategyType.CREDIT_SPREAD,
            margin=margin,
            max_loss=max_loss,
        )
        if not ok:
            return None
        if not structure_target_ok(structure_target_r, StrategyType.CREDIT_SPREAD):
            return None

        rspec = compute_r(
            StrategyType.CREDIT_SPREAD,
            width=width,
            credit=credit,
            contracts=contracts,
        )
        pid = str(uuid.uuid4())[:8]
        pos = Position(
            position_id=pid,
            strategy=StrategyType.CREDIT_SPREAD,
            category=category,
            symbol=symbol,
            contracts=contracts,
            entry_premium=credit,
            width=width,
            credit=credit,
            r_dollars=rspec.r_dollars,
            r_per_contract=rspec.r_per_contract,
            dte_at_entry=dte,
            structure_target_r=structure_target_r,
            margin=margin,
        )
        self._reserve_credit_margin(pos, margin)
        self.positions[pid] = pos
        self.account.opens_today += 1
        self.fills.append(Fill(pid, "sell_credit", contracts, credit))
        return pos

    def mark_exit(
        self,
        position_id: str,
        *,
        mark: float,
        dte: int,
        short_leg_distance_atr: float | None = None,
        short_delta: float | None = None,
        iv_entry: float | None = None,
        iv_now: float | None = None,
        at_structure_target: bool = False,
    ) -> Fill | None:
        pos = self.positions.get(position_id)
        if not pos:
            return None

        pr = pnl_in_r(
            pos.strategy,
            entry=pos.entry_premium,
            mark=mark,
            r_per_contract=pos.r_per_contract,
            width=pos.width,
            credit=pos.credit,
        )

        if pos.strategy == StrategyType.CREDIT_SPREAD:
            decision = credit_spread_exit(
                dte=dte,
                pnl_r=pr,
                short_leg_distance_atr=short_leg_distance_atr,
                short_delta=short_delta,
            )
        else:
            decision = long_or_debit_exit(
                dte=dte,
                pnl_r=pr,
                structure_target_r=pos.structure_target_r,
                remaining_fraction=pos.remaining_fraction,
                strategy=pos.strategy,
            )

        # IV 路径覆盖（标的到目标时）；不得覆盖硬止损
        if (
            iv_entry is not None
            and iv_now is not None
            and decision.reason != ExitReason.STOP_1R
        ):
            iv_dec = iv_path_override(
                iv_entry=iv_entry,
                iv_now=iv_now,
                at_structure_target=at_structure_target,
            )
            if iv_dec is not None:
                if iv_dec.action == ExitAction.HOLD and decision.action in (
                    ExitAction.CLOSE_ALL,
                    ExitAction.CLOSE_HALF,
                ):
                    # IV 绝对上升 >10：继续持有剩余（可打断止盈全平）
                    if "上升" in iv_dec.detail:
                        decision = iv_dec
                elif iv_dec.action == ExitAction.CLOSE_ALL:
                    decision = iv_dec

        if decision.action == ExitAction.HOLD:
            return None

        if decision.action == ExitAction.CLOSE_HALF and pos.contracts <= 1:
            decision = ExitDecision(
                ExitAction.CLOSE_ALL, decision.reason, "单张无法减半，1.5R 全平"
            )

        if decision.action == ExitAction.CLOSE_HALF:
            closed = pos.contracts // 2
            pnl = (mark - pos.entry_premium) * MULTIPLIER * closed
            self._release_premium(pos, 0.5)
            pos.remaining_fraction = 0.5
            pos.contracts = pos.contracts - closed
            halt_mod.apply_realized_pnl(self.account, pnl)
            if pnl > 0:
                halt_mod.record_win(self.account)
            fill = Fill(
                pos.position_id, "sell_half", closed, mark, pnl=pnl, reason=decision.detail
            )
            self.fills.append(fill)
            return fill

        # CLOSE_ALL
        if pos.strategy == StrategyType.CREDIT_SPREAD:
            pnl = (pos.credit - mark) * MULTIPLIER * pos.contracts
            self._release_credit_margin(pos)
        else:
            pnl = (mark - pos.entry_premium) * MULTIPLIER * pos.contracts
            self._release_premium(pos, 1.0)

        halt_mod.apply_realized_pnl(self.account, pnl)
        if decision.reason == ExitReason.STOP_1R:
            halt_mod.record_stop(self.account)
        elif pnl > 0:
            halt_mod.record_win(self.account)
        else:
            halt_mod.record_flat_or_loss_non_stop(self.account)

        fill = Fill(
            pos.position_id,
            "close",
            pos.contracts,
            mark,
            pnl=pnl,
            reason=decision.detail or (decision.reason.value if decision.reason else ""),
        )
        self.fills.append(fill)
        del self.positions[position_id]
        return fill
