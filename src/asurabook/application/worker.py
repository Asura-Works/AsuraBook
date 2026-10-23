"""OCR・upload worker。外部通信とAI実行は DB transaction の外で行い、結果は core が検証して採用する。"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..agents import bridge, receipt
from ..evidence.cloud import provider_from_config
from ..evidence.spool import open_object
from . import config as app_config
from .core import Core
from .session import Session

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "application/pdf": ".pdf"}


class Worker:
    def __init__(self, core: Core, session: Session, *, runner=bridge.run_receipt_extract):
        self.core = core
        self.session = session
        self.runner = runner  # 試験では偽の実行関数に差し替える

    def _call(self, name: str, payload: dict | None = None) -> dict:
        return self.core.execute(name, payload or {}, self.session)

    # -------------------------------------------------------- OCR

    def run_ocr_once(self) -> dict | None:
        claim = self._call("worker.ocr_claim")
        if not claim["ok"]:
            return {"claimed": False, "error": claim["error"]}
        job = claim["data"]
        if job is None:
            return None
        policy = bridge.AgentPolicy.from_config(app_config.load(self.core.paths)["ai"])
        job_dir = self.core.paths.jobs / job["agent_job_id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        name = "input-1" + _EXT.get(job["mime"], ".bin")
        try:
            data = open_object(self.core.paths.spool, job["local_ref"])
            (job_dir / name).write_bytes(data)  # 内部名でコピーした必要画像だけを渡す
            result = self.runner(policy, job_dir, [name], receipt.build_prompt([name]), receipt.compact_schema())
            outcome = {
                "status": result.status, "structured_output": result.structured_output, "usage": result.usage,
                "model": result.model, "cli_version": result.cli_version,
                "error_code": result.error_code, "error_message": result.error_message,
                "process_identity": " ".join(result.argv[:2]) if result.argv else None,
            }
        except bridge.AgentError as exc:
            status = {"AUTH_REQUIRED": "auth_required"}.get(exc.code, "failed")
            outcome = {"status": status, "error_code": exc.code, "error_message": exc.message}
        except Exception as exc:  # 予期しない失敗は成否不明として扱う
            outcome = {"status": "outcome_unknown", "error_code": "WORKER_EXCEPTION",
                       "error_message": type(exc).__name__}
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        done = self._call("worker.ocr_complete", {"ocr_job_id": job["ocr_job_id"],
                                                  "agent_job_id": job["agent_job_id"], "result": outcome})
        return done["data"] if done["ok"] else {"error": done["error"]}

    # -------------------------------------------------------- upload

    def run_upload_once(self) -> dict | None:
        storage = provider_from_config(app_config.load(self.core.paths), self.core.paths.cloud_local)
        if storage is None:
            return None
        claim = self._call("worker.upload_claim")
        if not claim["ok"]:
            return {"claimed": False, "error": claim["error"]}
        item = claim["data"]
        if item is None:
            return None
        payload = {"outbox_id": item["outbox_id"], "lease_token": item["lease_token"], "key": item["key"]}
        try:
            # 応答消失に備え、先に登録状態を照会する。同名objectを無条件に上書きしない
            found = [o for o in storage.head(item["key"]) if o.sha256 == item["sha256"]]
            if not found:
                data = open_object(self.core.paths.spool, item["local_ref"])
                storage.put(item["key"], data, item["sha256"])
                found = [o for o in storage.head(item["key"]) if o.sha256 == item["sha256"]]
            if found:
                payload |= {"outcome": "verified", "remote_version": found[-1].version,
                            "remote_sha256": found[-1].sha256}
            else:
                payload |= {"outcome": "failed", "error": "upload後の照会で hash が確認できません"}
        except Exception as exc:
            payload |= {"outcome": "failed", "error": f"{type(exc).__name__}: {exc}"}
        done = self._call("worker.upload_complete", payload)
        return done["data"] if done["ok"] else {"error": done["error"]}

    def run_once(self) -> dict:
        return {"ocr": self.run_ocr_once(), "upload": self.run_upload_once()}

    def drain(self, limit: int = 50) -> list[dict]:
        out = []
        for _ in range(limit):
            r = self.run_once()
            if r["ocr"] is None and r["upload"] is None:
                break
            out.append(r)
            if (r["ocr"] or {}).get("claimed") is False and (r["upload"] or {}).get("claimed") is False:
                break
        return out


def default_jobs_dir(core: Core) -> Path:
    return core.paths.jobs
