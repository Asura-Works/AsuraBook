"""Codex CLI adapter (21章5節 Codex job)。Claude が使えない場合に、利用者が設定したときだけ使う代替。

codex exec を read-only sandbox・shell tool 無効・web search 無効・利用者設定を読まない構成で起動し、
画像は --image で渡す。進行の JSONL と最終の業務JSON (--output-last-message) を別々に扱う。
この adapter は偽CLIで起動契約を検証しただけで、実際の Codex CLI では未検証。
"""

from __future__ import annotations

import json
from pathlib import Path

from .bridge import AgentError, AgentPolicy, RunResult, _run, child_env, resolve_exe

REQUIRED_FLAGS = ("--sandbox", "--output-schema", "--output-last-message", "--json", "--image")
OPTIONAL_FLAGS = ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config")
SUBSCRIPTION_METHODS = ("chatgpt",)


def policy_from_config(ai: dict) -> AgentPolicy:
    return AgentPolicy(provider="codex_cli", exe=ai.get("codex_exe"), auth_mode=ai.get("auth_mode", "subscription"),
                       allowed_auth_methods=SUBSCRIPTION_METHODS, allowed_api_providers=("openai",),
                       model=ai.get("codex_model"), timeout_seconds=int(ai.get("timeout_seconds") or 180))


def check_auth(policy: AgentPolicy, *, exe: str, env: dict[str, str]) -> dict:
    code, out, err, timed_out = _run([exe, "login", "status"], env=env, timeout=30)
    text = (out + err).decode("utf-8", "replace")
    lowered = text.lower()
    if timed_out:
        raise AgentError("AUTH_STATUS_UNKNOWN", "Codex の認証状態の確認が timeout しました")
    if "not logged in" in lowered:
        raise AgentError("AUTH_REQUIRED", "Codex にログインしていません")
    if "chatgpt" in lowered:
        method = "chatgpt"
    elif "api key" in lowered or "api_key" in lowered:
        method = "api_key"
    else:
        raise AgentError("AUTH_STATUS_UNKNOWN", "Codex の認証方式を判定できません。安全のため停止します")
    allowed = policy.allowed_auth_methods if policy.auth_mode == "subscription" else ("api_key",)
    if method not in allowed:
        raise AgentError("AUTH_MODE_MISMATCH", "Codex の実効認証が設定modeと一致しません。API課金への切替を防ぐため停止します",
                         {"auth_method": method, "auth_mode": policy.auth_mode})
    return {"auth_method": method}


def _flags(exe: str, env: dict[str, str]) -> dict[str, bool]:
    _, out, _, _ = _run([exe, "exec", "--help"], env=env, timeout=30)
    text = out.decode("utf-8", "replace")
    return {f: f in text for f in REQUIRED_FLAGS + OPTIONAL_FLAGS}


def doctor(policy: AgentPolicy) -> dict:
    exe = resolve_exe(policy)
    env = child_env(policy)
    _, out, _, _ = _run([exe, "--version"], env=env, timeout=30)
    flags = _flags(exe, env)
    result = {"provider": "codex_cli", "exe": exe, "version": out.decode("utf-8", "replace").strip(), "flags": flags,
              "verified_with_real_cli": False}
    missing = [f for f in REQUIRED_FLAGS if not flags[f]]
    if missing:
        return result | {"ok": False, "error": {"code": "AGENT_FLAGS_UNSUPPORTED", "missing": missing}}
    try:
        return result | {"ok": True, "auth": check_auth(policy, exe=exe, env=env)}
    except AgentError as exc:
        return result | {"ok": False, "error": {"code": exc.code, "message": exc.message}}


def run_receipt_extract(policy: AgentPolicy, job_dir: Path, file_names: list[str], prompt: str,
                        schema_json: str) -> RunResult:
    exe = resolve_exe(policy)
    env = child_env(policy)
    check_auth(policy, exe=exe, env=env)
    _, version_out, _, _ = _run([exe, "--version"], env=env, timeout=30)
    flags = _flags(exe, env)
    missing = [f for f in REQUIRED_FLAGS if not flags[f]]
    if missing:
        raise AgentError("AGENT_FLAGS_UNSUPPORTED", "採用 Codex CLI 版が必要な flag に対応していません", {"missing": missing})
    for name in file_names:
        if "/" in name or "\\" in name or name.startswith("."):
            raise AgentError("INVALID_INPUT", "job入力は内部名のファイルだけです")
    # schema と最終出力は画像と別のフォルダへ置く (job の cwd には必要画像だけを置く)
    meta = job_dir.parent / f"{job_dir.name}-meta"
    meta.mkdir(parents=True, exist_ok=True)
    schema_path = meta / "schema.json"
    schema_path.write_text(schema_json, encoding="utf-8")
    last_message = meta / "last_message.json"
    argv = [exe, "exec", "--sandbox", "read-only", "--json", "--output-schema", str(schema_path),
            "--output-last-message", str(last_message), "-c", "features.shell_tool=false",
            "-c", 'web_search="disabled"']
    for flag in OPTIONAL_FLAGS:
        if flags[flag]:
            argv.append(flag)
    if policy.model:
        argv += ["--model", policy.model]
    for name in file_names:
        argv += ["--image", str(job_dir / name)]
    argv.append("-")  # prompt は stdin
    code, out, err, timed_out = _run(argv, env=env, cwd=job_dir, stdin=prompt, timeout=policy.timeout_seconds,
                                     max_stdout=policy.max_stdout_bytes, max_stderr=policy.max_stderr_bytes)
    display = [a if a != str(schema_path) else "<schema>" for a in argv]
    cli_version = version_out.decode("utf-8", "replace").strip()[:100] or None
    usage, error_event = None, None
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or event.get("msg", {}).get("type") or "")
        if "usage" in event:
            usage = event["usage"]
        if kind in ("error", "turn.failed") or kind.endswith(".error"):
            error_event = event
    stderr_tail = err.decode("utf-8", "replace")[-2000:]
    common = {"argv": display, "cli_version": cli_version, "usage": usage, "stderr_tail": stderr_tail, "exit_code": code}
    try:
        if timed_out:
            return RunResult("timed_out", error_code="TIMEOUT",
                             error_message="timeout。Provider側で処理・利用量が発生した可能性があります", **common)
        if error_event is not None:
            message = json.dumps(error_event, ensure_ascii=False)[:500]
            lowered = message.lower()
            if any(k in lowered for k in ("rate limit", "usage limit", "quota")):
                return RunResult("quota_wait", error_code="QUOTA_WAIT", error_message=message, **common)
            return RunResult("failed", error_code="AGENT_ERROR", error_message=message, **common)
        if not last_message.exists():
            status = "failed" if code not in (0, None) else "outcome_unknown"
            return RunResult(status, error_code="NO_FINAL_MESSAGE", error_message=(stderr_tail.strip().splitlines()
                                                                                    or ["最終出力がありません"])[-1][:500],
                             **common)
        try:
            output = json.loads(last_message.read_text(encoding="utf-8"))
        except ValueError:
            return RunResult("invalid_output", error_code="FINAL_NOT_JSON",
                             error_message="最終出力が JSON ではありません (自然文からは抽出しません)", **common)
        if code != 0:
            return RunResult("failed", error_code="NONZERO_EXIT", error_message=f"exit code {code}", **common)
        return RunResult("succeeded", structured_output=output, model=policy.model, **common)
    finally:
        for p in (schema_path, last_message):
            if p.exists():
                p.unlink()
        try:
            meta.rmdir()
        except OSError:
            pass
