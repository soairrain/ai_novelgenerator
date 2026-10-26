"""第七章组合 Greeks 监控。"""

from __future__ import annotations

from dataclasses import dataclass

from options_buyer.core.types import (
    MULTIPLIER,
    NET_DELTA_MAX,
    NET_THETA_WARN,
    NET_VEGA_MAX,
    StrategyType,
)


@dataclass
class LegGreeks:
    delta: float
    vega: float
    theta: float
    gamma: float = 0.0
    contracts: int = 1
    strategy: StrategyType = StrategyType.LONG
    spot: float = 0.0


@dataclass(frozen=True)
class PortfolioGreeks:
    net_delta_usd: float
    net_vega_usd: float
    net_theta: float
    net_gamma: float
    ok: bool
    reasons: tuple[str, ...]


def _weight(strategy: StrategyType) -> float:
    # 价差仓位按 50% 折算；裸买 100%
    if strategy in (StrategyType.DEBIT_SPREAD, StrategyType.CREDIT_SPREAD):
        return 0.5
    return 1.0


def aggregate(legs: list[LegGreeks], equity: float) -> PortfolioGreeks:
    nd = nv = nt = ng = 0.0
    for leg in legs:
        w = _weight(leg.strategy)
        m = leg.contracts * MULTIPLIER * w
        nd += leg.delta * m * leg.spot
        nv += leg.vega * m
        nt += leg.theta * m
        ng += leg.gamma * m

    reasons: list[str] = []
    if abs(nd) > equity * NET_DELTA_MAX:
        reasons.append(f"净 Delta 美元敞口 {nd:.0f} 超过账户 10%")
    if abs(nv) > equity * NET_VEGA_MAX:
        reasons.append(f"净 Vega {nv:.0f} 超过账户 0.5%/IV点")
    if nt > 0:
        reasons.append("净 Theta 为正，不加买方仓位")
    elif abs(nt) > equity * NET_THETA_WARN:
        reasons.append("净 Theta 负向超过账户 0.1%/天，检查过度持有")

    # Theta 警告不硬阻断买方以外的场景；净 Theta>0 与 Delta/Vega 超限硬阻断
    hard = [r for r in reasons if not r.startswith("净 Theta 负向")]
    return PortfolioGreeks(nd, nv, nt, ng, ok=len(hard) == 0, reasons=tuple(reasons))
