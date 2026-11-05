"""Data loaders: cached CSV and Yahoo Finance (daily / intraday). Live option data comes from Upstox
(kavach.brokers.upstox)."""
import json
import os
import urllib.request
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("KAVACH_DATA", Path(__file__).resolve().parent.parent / "data"))
UA = {"User-Agent": "Mozilla/5.0 (kavach research)"}


def _yahoo(symbol: str, rng: str, interval: str) -> pd.DataFrame:
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval={interval}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        j = json.load(r)["chart"]["result"][0]
    q = j["indicators"]["quote"][0]
    idx = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx)
    return df.dropna(subset=["open", "high", "low", "close"])


def refresh_daily() -> None:
    """Download ~10y of Nifty 50 and India VIX daily bars into data/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for sym, name in (("%5ENSEI", "nifty_daily"), ("%5EINDIAVIX", "indiavix_daily")):
        df = _yahoo(sym, "10y", "1d")
        df.index = pd.to_datetime(df.index.date)
        df = df[~df.index.duplicated(keep="last")]
        df.to_csv(DATA_DIR / f"{name}.csv")


def load_daily() -> pd.DataFrame:
    """Nifty OHLC joined with India VIX close (column 'vix', in vol points)."""
    n = pd.read_csv(DATA_DIR / "nifty_daily.csv", index_col=0, parse_dates=True)
    v = pd.read_csv(DATA_DIR / "indiavix_daily.csv", index_col=0, parse_dates=True)
    df = n[["open", "high", "low", "close"]].join(v["close"].rename("vix"), how="inner")
    df = df[(df["high"] >= df["low"]) & (df["vix"] > 0)]
    return df[~df.index.duplicated(keep="last")].sort_index()


def load_intraday_yahoo(interval: str = "5m", rng: str = "60d") -> pd.DataFrame:
    """Nifty intraday bars from Yahoo (5m bars: last ~60 days only)."""
    cache = DATA_DIR / f"nifty_{interval}.csv"
    try:
        df = _yahoo("%5ENSEI", rng, interval)
        df.to_csv(cache)
    except Exception:
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
    return df


def yahoo_last(symbol: str = "%5ENSEI") -> float:
    """Latest 1-minute close from Yahoo (used by the synthetic quote source)."""
    df = _yahoo(symbol, "1d", "1m")
    return float(df["close"].iloc[-1])
