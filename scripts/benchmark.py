"""大量データでの応答時間を測る (01章4節: 10万仕訳で日付索引検索を概ね1秒以内という目標)。

試験データ生成だけは速度のため SQL で直接作る (確定仕訳と同じ制約・trigger を通る)。測定は Core.execute 経由。
結果は測定した端末・規模の参考値で、Windows 実機の値ではない。

uv run python scripts/benchmark.py --entries 100000 [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

import asurabook.application.commands  # noqa: F401
from asurabook.application.core import Core
from asurabook.application.session import Session
from asurabook.domain.values import new_id, utc_now

OWNER = Session("bench:owner", "owner", "gui")
EXPENSES = ["514", "519", "513", "527", "599", "528", "512"]


def generate(core: Core, n: int, year: int) -> float:
    conn = core.writer()
    org = conn.execute("SELECT id FROM org").fetchone()[0]
    fy = conn.execute("SELECT id FROM fiscal_years WHERE year=?", (year,)).fetchone()[0]
    acc = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM accounts")}
    rng = random.Random(42)
    now = utc_now()
    start = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    for i in range(n):
        eid = new_id()
        day = 1 + i * 365 // n
        date = time.strftime("%Y-%m-%d", time.strptime(f"{year} {day}", "%Y %j"))
        amount = rng.randint(100, 200000)
        if i % 5 == 0:
            debit, credit, desc = "110", "400", f"売上 案件{i}"
        else:
            debit, credit, desc = rng.choice(EXPENSES), rng.choice(["100", "110", "210"]), f"経費 {i} 取引先{i % 997}"
        conn.execute("INSERT INTO journal_entries (id, org_id, fiscal_year_id, journal_date, description, status, "
                     "created_by, created_at, updated_at) VALUES (?,?,?,?,?, 'draft', 'bench', ?, ?)",
                     (eid, org, fy, date, desc, now, now))
        conn.execute("INSERT INTO journal_lines (id, entry_id, line_no, account_id, side, amount_jpy) VALUES (?,?,1,?,"
                     "'debit',?)", (new_id(), eid, acc[debit], amount))
        conn.execute("INSERT INTO journal_lines (id, entry_id, line_no, account_id, side, amount_jpy) VALUES (?,?,2,?,"
                     "'credit',?)", (new_id(), eid, acc[credit], amount))
        conn.execute("UPDATE journal_entries SET status='validated', validation_hash='bench' WHERE id=?", (eid,))
        conn.execute("UPDATE journal_entries SET status='posted', number=?, posted_by='bench', posted_at=? WHERE id=?",
                     (i + 1, now, eid))
    conn.execute("UPDATE org SET ledger_revision = ledger_revision + 1")
    conn.execute("COMMIT")
    conn.execute("ANALYZE")
    return time.perf_counter() - start


def measure(core: Core, name: str, payload: dict, repeat: int = 5) -> dict:
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        resp = core.execute(name, payload, OWNER)
        times.append(time.perf_counter() - t)
        if not resp["ok"]:
            raise SystemExit(f"{name} failed: {resp['error']}")
    return {"command": name, "payload": payload, "median_s": round(statistics.median(times), 4),
            "max_s": round(max(times), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", type=int, default=100_000)
    ap.add_argument("--data-dir")
    ap.add_argument("--json-out")
    args = ap.parse_args()
    data = Path(args.data_dir or tempfile.mkdtemp(prefix="asurabook-bench-"))
    core = Core(data)
    year = 2026
    assert core.execute("org.init", {"display_name": "bench", "years": [year]}, OWNER)["ok"]
    gen = generate(core, args.entries, year)
    cases = [
        ("journal.list", {"year": year, "date_from": f"{year}-06-01", "date_to": f"{year}-06-07", "limit": 100}),
        ("journal.list", {"year": year, "date_from": f"{year}-06-01", "limit": 100}),
        ("journal.list", {"year": year, "text": "取引先123", "limit": 100}),
        ("journal.list", {"year": year, "account": "514", "date_from": f"{year}-11-01", "limit": 100}),
        ("journal.list", {"year": year, "amount_jpy": 12345, "limit": 100}),
        ("report.trial_balance", {"year": year}),
        ("report.profit_loss", {"year": year}),
        ("report.general_ledger", {"year": year, "account": "110", "date_from": f"{year}-12-01"}),
        ("dashboard.summary", {"year": year}),
        ("monitor.check", {"year": year}),
    ]
    results = [measure(core, name, payload, repeat=1 if name == "monitor.check" else 5) for name, payload in cases]
    import sqlite3 as s3
    report = {
        "entries": args.entries, "lines": args.entries * 2, "generate_s": round(gen, 1),
        "db_size_mb": round(core.paths.db.stat().st_size / 1024 / 1024, 1),
        "environment": {"python": sys.version.split()[0], "sqlite": s3.sqlite_version, "platform": platform.platform(),
                        "processor": platform.processor() or platform.machine()},
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    core.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
