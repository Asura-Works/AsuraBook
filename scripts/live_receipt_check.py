"""実際の Claude Code CLI でレシート画像を読み取る受入確認 (21章13節)。

利用者の Claude CLI 認証 (subscription) を使い、画像をモデル提供先へ送信する。
架空の試験画像だけを使うこと。

uv run python scripts/live_receipt_check.py IMAGE [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import taxdesk.application.commands  # noqa: F401
from taxdesk.agents import bridge
from taxdesk.application import config as app_config
from taxdesk.application.core import Core
from taxdesk.application.session import Session
from taxdesk.application.worker import Worker


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--data-dir")
    args = ap.parse_args()
    data_dir = Path(args.data_dir or tempfile.mkdtemp(prefix="taxdesk-live-"))
    core = Core(data_dir)
    owner = Session(principal="live:owner", profile="owner", entry_point="gui")
    if not core.db_exists():
        assert core.execute("org.init", {"display_name": "受入確認", "years": [2026]}, owner)["ok"]
    policy = bridge.AgentPolicy.from_config(app_config.load(core.paths)["ai"])
    doctor = bridge.doctor(policy)
    print("doctor:", json.dumps(doctor, ensure_ascii=False, indent=2))
    if not doctor.get("ok"):
        return 2
    added = core.execute("evidence.add", {"path": args.image}, owner)
    assert added["ok"], added
    result = Worker(core, owner).run_ocr_once()
    print("worker:", json.dumps(result, ensure_ascii=False, indent=2))
    shown = core.execute("evidence.show", {"document_id": added["data"]["id"]}, owner)["data"]
    job = shown["ocr_jobs"][-1]
    print("extraction:", json.dumps(job["extraction"], ensure_ascii=False, indent=2))
    agent = core.execute("ai.jobs", {}, owner)["data"][0]
    print("agent_job:", json.dumps(agent, ensure_ascii=False, indent=2))
    return 0 if job["state"] in ("extracted", "needs_review") else 1


if __name__ == "__main__":
    sys.exit(main())
