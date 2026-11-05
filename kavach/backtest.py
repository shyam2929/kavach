"""Weekly-cycle option backtester on daily Nifty + India VIX data.

Honest-modelling choices (all conservative or explicit):
- Entry at the close of the first trading day after the previous weekly expiry.
- Weekly ATM IV = VIX * iv_mult. VIX is a 30-day IV; weekly ATM IV differs. iv_mult is the single
  most important unknown. Run sensitivity at 0.85/0.90/1.00, and calibrate it with real chain data
  (the paper runner logs Upstox ATM IV vs VIX every step: `cli calibrate`) before trusting any number.
- Smile from pricing.skew_iv. Real smiles can be steeper on stress days.
- Stops/targets are checked at the day's HIGH and LOW (adverse side first, stop wins ties), with
  extra half-day of time value, so intraday excursions are not ignored.
- Every leg pays slippage on entry and exit; full statutory charges; STT on ITM longs at expiry.
- The volatility forecast is walk-forward (no look-ahead); regime thresholds were fixed ex ante.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel, DEFAULT_COSTS
from .expiry import weekly_expiries
from .pricing import black, forward, skew_iv, LOT_SIZE
from .regime import regime_frame
from .sizing import capital_fraction, lots_for
from .volmodel import WalkForwardHAR

TD = 252.0


@dataclass
class BTConfig:
    equity0: float = 100_000.0
    iv_mult: float = 1.0
    risk_per_trade: float = 0.06
    hard_cap: float = 0.15
    max_lots: int = 10
    evidence_gate: bool = True
    costs: CostModel = field(default_factory=lambda: DEFAULT_COSTS)
    start: str = "2018-01-01"
    end: str | None = None
    halt_weeks: int = 4


def _value(legs, S, t, atm_iv):
    """Mark value per lot-unit (points): long legs +, short legs -."""
    F = forward(S, t)
    return sum(l.qty * black(F, l.K, t, skew_iv(atm_iv, F, l.K, t), l.cp) for l in legs)


def _leg_prices(legs, S, t, atm_iv):
    F = forward(S, t)
    return [black(F, l.K, t, skew_iv(atm_iv, F, l.K, t), l.cp) for l in legs]


def _payoff_extremes(legs, net_credit, F):
    grid = np.linspace(F * 0.6, F * 1.4, 4001)
    pay = np.zeros_like(grid)
    for l in legs:
        intr = np.maximum(grid - l.K, 0) if l.cp == "C" else np.maximum(l.K - grid, 0)
        pay += l.qty * intr
    pnl = pay + net_credit
    return float(-pnl.min()), float(pnl.max())     # max loss (pts, >0), max profit (pts)


def run(df: pd.DataFrame, strategy, cfg: BTConfig = BTConfig(), rv_fcst: pd.Series | None = None):
    df = df.copy()
    if rv_fcst is None:
        rv_fcst = WalkForwardHAR().forecast_series(df)
    reg = regime_frame(df)
    exps = weekly_expiries(df.index)
    days = df.index
    pos_of = {d: i for i, d in enumerate(days)}
    c = cfg.costs

    equity, eq_curve, trades, period_rets = cfg.equity0, [cfg.equity0], [], []
    trade_rets, peak = [], cfg.equity0
    pause_left, halts = 0, 0
    exps = exps[(exps >= pd.Timestamp(cfg.start)) & ((exps <= pd.Timestamp(cfg.end)) if cfg.end else True)]

    for e_prev, e_next in zip(exps[:-1], exps[1:]):
        i_entry = pos_of[e_prev] + 1
        i_exp = pos_of[e_next]
        if i_entry >= i_exp:
            period_rets.append(0.0); eq_curve.append(equity); continue
        d0 = days[i_entry]
        row = df.iloc[i_entry]
        fc = rv_fcst.iloc[i_entry]
        if np.isnan(fc):
            period_rets.append(0.0); eq_curve.append(equity); continue
        t0 = (i_exp - i_entry) / TD
        atm_iv = row.vix / 100 * cfg.iv_mult
        plan = strategy.plan(row.close, t0, atm_iv, fc, bool(reg["stress"].iloc[i_entry]))
        if plan is None:
            period_rets.append(0.0); eq_curve.append(equity); continue

        mids = _leg_prices(plan.legs, row.close, t0, atm_iv)
        fills = [p + c.slippage(p) if l.qty > 0 else max(0.05, p - c.slippage(p))
                 for l, p in zip(plan.legs, mids)]
        net_credit = -sum(l.qty * f for l, f in zip(plan.legs, fills))   # >0 credit, <0 debit
        F0 = forward(row.close, t0)
        max_loss, max_profit = _payoff_extremes(plan.legs, net_credit, F0)
        is_credit = net_credit > 0
        dd = equity / peak - 1
        frac = capital_fraction(trade_rets, dd) if cfg.evidence_gate else 1.0
        if cfg.evidence_gate and frac == 0.0:
            # governor halt at -20% drawdown. Live: human review required.
            # Backtest: model the review as a `halt_weeks` pause, then reset the peak.
            if pause_left == 0:
                pause_left, halts = cfg.halt_weeks, halts + 1
            pause_left -= 1
            if pause_left == 0:
                peak = equity
            period_rets.append(0.0); eq_curve.append(equity); continue
        margin_pts = max_loss * 1.2 if is_credit else -net_credit
        lots = lots_for(max_loss * LOT_SIZE, equity, cfg.risk_per_trade, frac, cfg.max_lots, cfg.hard_cap)
        while lots > 0 and lots * margin_pts * LOT_SIZE > equity * 0.95:
            lots -= 1
        if lots == 0:
            period_rets.append(0.0); eq_curve.append(equity); continue
        q = lots * LOT_SIZE

        exit_pts, exit_day, reason, exit_prices = None, None, "expiry", None
        stop_pts = -plan.stop * (max_loss if is_credit else -net_credit)
        tp_pts = plan.take_profit * (max_profit if is_credit else -net_credit)
        for j in range(i_entry + 1, i_exp + 1):
            d = days[j]
            r = df.iloc[j]
            remaining = i_exp - j
            iv = max(r.vix, df.vix.iloc[j - 1]) / 100 * cfg.iv_mult
            t_close, t_mid = remaining / TD, (remaining + 0.5) / TD
            # adverse extreme first: for a short-vol book both extremes are adverse; check both
            hit = None
            for S in (r.low, r.high):
                px = _leg_prices(plan.legs, S, t_mid, iv)
                liq = sum(l.qty * (p - c.slippage(p) if l.qty > 0 else p + c.slippage(p))
                          for l, p in zip(plan.legs, px))
                pnl = net_credit + liq
                if pnl <= stop_pts:
                    hit = ("stop", pnl, px); break
                if pnl >= tp_pts and hit is None:
                    hit = ("target", pnl, px)
            if hit is None and j == i_exp:
                break                                   # settle at expiry close below
            if hit is None and remaining <= plan.exit_days_before:
                px = _leg_prices(plan.legs, r.close, t_close, iv)
                liq = sum(l.qty * (p - c.slippage(p) if l.qty > 0 else p + c.slippage(p))
                          for l, p in zip(plan.legs, px))
                hit = ("time", net_credit + liq, px)
            if hit:
                reason, exit_pts, exit_prices = hit
                exit_day = d
                break

        buy_turn = sum(f for l, f in zip(plan.legs, fills) if l.qty > 0) * q
        sell_turn = sum(f for l, f in zip(plan.legs, fills) if l.qty < 0) * q
        n_orders = len(plan.legs)
        exercised = 0.0
        if exit_pts is None:     # settle at expiry close
            S = df.close.iloc[i_exp]
            intr = [max(S - l.K, 0) if l.cp == "C" else max(l.K - S, 0) for l in plan.legs]
            exit_pts = net_credit + sum(l.qty * x for l, x in zip(plan.legs, intr))
            exercised = sum(x for l, x in zip(plan.legs, intr) if l.qty > 0) * q
            exit_day = days[i_exp]
        else:
            n_orders *= 2
            buy_turn += sum(p for l, p in zip(plan.legs, exit_prices) if l.qty < 0) * q
            sell_turn += sum(p for l, p in zip(plan.legs, exit_prices) if l.qty > 0) * q
        charges = c.charges(buy_turn, sell_turn, n_orders, exercised)
        pnl_rs = exit_pts * q - charges
        ret = pnl_rs / equity
        trades.append({"entry": d0, "exit": exit_day, "strategy": plan.name, "lots": lots,
                       "spot": row.close, "vix": row.vix, "rv_fcst": fc, "credit_pts": net_credit,
                       "max_loss_pts": max_loss, "exit_reason": reason, "gross_pts": exit_pts,
                       "charges": charges, "pnl": pnl_rs, "ret": ret, "cap_frac": frac,
                       "legs": [(l.cp, l.K, l.qty) for l in plan.legs]})
        trade_rets.append(ret)
        equity += pnl_rs
        peak = max(peak, equity)
        period_rets.append(ret)
        eq_curve.append(equity)
        if equity <= 0:
            break

    tdf = pd.DataFrame(trades)
    return {"trades": tdf, "equity": np.array(eq_curve), "period_returns": np.array(period_rets),
            "weeks": len(period_rets), "halts": halts}
