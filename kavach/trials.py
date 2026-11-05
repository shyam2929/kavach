"""Trial ledger: every backtest run is recorded, so the system knows how many things were tried.
The count feeds the Deflated Sharpe Ratio. You (or an AI agent) cannot run 10,000 variants,
keep the best, and present it as skill: the ledger charges you for all 10,000."""
import hashlib
import json
import time
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "state" / "trials.jsonl"


def record(family: str, params: dict, metrics: dict, ledger: Path = LEDGER) -> int:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]
    row = {"ts": time.time(), "family": family, "key": key, "params": params, "metrics": metrics}
    with ledger.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return count(family, ledger)


def count(family: str, ledger: Path = LEDGER) -> int:
    """Number of DISTINCT parameter sets ever tried in this strategy family."""
    if not ledger.exists():
        return 0
    keys = set()
    for line in ledger.read_text().splitlines():
        row = json.loads(line)
        if row["family"] == family:
            keys.add(row["key"])
    return len(keys)
