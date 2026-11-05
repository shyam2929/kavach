"""Indian index-option transaction costs (NSE, as of Nov 2026).

Sources checked Nov 2026:
- STT on sale of options: 0.15% of premium (Budget 2026, effective 1 Apr 2026)
- STT on exercised (ITM-at-expiry) long options: 0.15% of intrinsic value, paid by buyer
- NSE transaction charge: 0.03503% of premium turnover
- SEBI fee: Rs 10 / crore of turnover
- Stamp duty: 0.003% of buy-side premium
- GST: 18% on (brokerage + exchange txn + SEBI fee)
- Brokerage: flat Rs 20 per executed F&O order (Upstox / discount-broker plan; check your plan)

Every constant is a parameter so you can update it when a circular changes it.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0
    stt_sell: float = 0.0015
    stt_exercise: float = 0.0015
    exch_txn: float = 0.0003503
    sebi: float = 10 / 1e7
    ipft: float = 50 / 1e7          # NSE investor-protection fund on option premium
    stamp_buy: float = 0.00003
    gst: float = 0.18
    # slippage: half bid-ask + impact, per unit of premium, per side
    slip_abs: float = 0.25          # rupees per option unit
    slip_pct: float = 0.005         # 0.5% of price
    stress: float = 1.0             # multiply ALL costs (use 2.0 for stress tests)

    def slippage(self, price: float) -> float:
        return self.stress * max(self.slip_abs, self.slip_pct * price)

    def charges(self, buy_turnover: float, sell_turnover: float, n_orders: int,
                exercised_intrinsic_value: float = 0.0) -> float:
        """Statutory + broker charges in rupees (excludes slippage)."""
        turnover = buy_turnover + sell_turnover
        brokerage = self.brokerage_per_order * n_orders
        exch = self.exch_txn * turnover
        sebi = self.sebi * turnover
        ipft = self.ipft * turnover
        stt = self.stt_sell * sell_turnover + self.stt_exercise * exercised_intrinsic_value
        stamp = self.stamp_buy * buy_turnover
        gst = self.gst * (brokerage + exch + sebi + ipft)
        return self.stress * (brokerage + exch + sebi + ipft + stt + stamp + gst)


DEFAULT_COSTS = CostModel()
