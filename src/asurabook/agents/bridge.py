"""AgentBridge: claude -p / codex exec の起動契約 (21章5〜7節)。

- 固定した実体exeを引数配列 (shell=False) で起動し、prompt は stdin で渡す。
- 子環境は mode別 allowlist から作る。subscription mode では API key・bearer token・
  endpoint override・cloud-provider 選択を引き継がない。
- 起動前に同じ exe・環境で実効認証を確認し、設定 mode と違えば AUTH_MODE_MISMATCH で止める。
- exit code 0 だけで採用せず、JSON wrapper の structured_output を取り出して検証する。
- 自然文からJSONらしい部分を切り出さない。画像内の命令はデータとして扱う。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_VERSION = "agent_policy/1.0"

# 子processへ渡してよい環境変数 (認証経路を変えないもの)
BASE_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    # 利用者のネットワーク設定 (送信先・認証を変えない)
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)
API_KEY_ENV = ("ANTHROPIC_API_KEY",)
# subscription mode で絶対に渡さないもの (allowlist 方式なので通常は入らないが明示的に検査する)
FORBIDDEN_IN_SUBSCRIPTION = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_API_KEY",
    "CODEX_API_KEY", "OPENAI_BASE_URL",
)

REQUIRED_CLAUDE_FLAGS = ("--output-format", "--json-schema", "--tools", "--no-session-persistence")
OPTIONAL_CLAUDE_FLAGS = ("--restricted", "--strict-mcp-config", "--disallowedTools", "--max-turns")


class AgentError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class AgentPolicy:
    provider: str = "claude_cli"
    exe: str | None = None
    auth_mode: str = "subscription"  # subscription | api_key
    allowed_auth_methods: tuple[str, ...] = ("claude.ai", "oauth_token")
    allowed_api_providers: tuple[str, ...] = ("firstParty",)
    model: str | None = None
    timeout_seconds: int = 180
    max_stdout_bytes: int = 10 * 1024 * 1024
    max_stderr_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_config(cls, cfg: dict | None) -> "AgentPolicy":
        cfg = cfg or {}
        policy = cls()
        for key in ("provider", "exe", "auth_mode", "model", "timeout_seconds"):
            if cfg.get(key) is not None:
                setattr(policy, key, cfg[key])
        if cfg.get("allowed_auth_methods"):
            policy.allowed_auth_methods = tuple(cfg["allowed_auth_methods"])
        if cfg.get("allowed_api_providers"):
            policy.allowed_api_providers = tuple(cfg["allowed_api_providers"])
        return policy


@dataclass
class RunResult:
    status: str  # succeeded | failed | invalid_output | timed_out | auth_required | quota_wait | outcome_unknown
    structured_output: Any = None
    wrapper: dict | None = None
    exit_code: int | None = None
    stderr_tail: str = ""
    model: str | None = None
    usage: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    argv: list[str] = field(default_factory=list)
    cli_version: str | None = None


def resolve_exe(policy: AgentPolicy) -> str:
    """固定した実体exeを解決する。.cmd/.bat の shim は使わない。"""
    name = "claude" if policy.provider == "claude_cli" else "codex"
    exe = policy.exe or shutil.which(name)
    if not exe:
        raise AgentError("AGENT_NOT_INSTALLED", f"{name} が見つかりません。公式手順で導入し設定してください")
    real = os.path.realpath(exe)
    if not os.path.isfile(real):
        raise AgentError("AGENT_NOT_INSTALLED", f"{exe} は実行ファイルではありません")
    if sys.platform == "win32" and real.lower().endswith((".cmd", ".bat")):
        raise AgentError("AGENT_SHIM_REJECTED", "cmd.exe 経由の shim は使いません。実体の .exe を設定してください",
                         {"exe": real})
    return real


def child_env(policy: AgentPolicy, parent: dict[str, str] | None = None) -> dict[str, str]:
    parent = dict(os.environ if parent is None else parent)
    allowed = set(BASE_ENV_ALLOWLIST)
    if policy.auth_mode == "api_key":
        allowed |= set(API_KEY_ENV if policy.provider == "claude_cli" else ("OPENAI_API_KEY", "CODEX_API_KEY"))
    env = {k: v for k, v in parent.items() if k in allowed}
    if policy.auth_mode == "subscription":
        leaked = [k for k in FORBIDDEN_IN_SUBSCRIPTION if k in env]
        if leaked:  # allowlist の設定誤りを検出する防御
            raise AgentError("AUTH_MODE_MISMATCH", "subscription mode に API 認証の環境変数が混入しています",
                             {"variables": leaked})
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _run(argv: list[str], *, env: dict[str, str], cwd: Path | None = None, stdin: str | None = None,
         timeout: int, max_stdout: int = 10 * 1024 * 1024, max_stderr: int = 2 * 1024 * 1024
         ) -> tuple[int | None, bytes, bytes, bool]:
    """子process群を管理して実行する。timeout 時は自分が起動した process group だけを終了する。"""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=str(cwd) if cwd else None, shell=False, **kwargs)
    try:
        out, err = proc.communicate(input=None if stdin is None else stdin.encode("utf-8"), timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _terminate(proc)
        out, err = proc.communicate()
        timed_out = True
    return proc.returncode, out[:max_stdout + 1], err[-max_stderr:], timed_out


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def doctor(policy: AgentPolicy) -> dict:
    """採用CLI版・対応flag・実効認証を検査する (21章5・6節)。"""
    exe = resolve_exe(policy)
    env = child_env(policy)
    code, out, err, _ = _run([exe, "--version"], env=env, timeout=30)
    version = out.decode("utf-8", "replace").strip()
    code, help_out, _, _ = _run([exe, "--help"], env=env, timeout=30)
    help_text = help_out.decode("utf-8", "replace")
    flags = {f: (f in help_text) for f in REQUIRED_CLAUDE_FLAGS + OPTIONAL_CLAUDE_FLAGS}
    result: dict[str, Any] = {"provider": policy.provider, "exe": exe, "version": version, "flags": flags,
                              "auth_mode": policy.auth_mode}
    missing = [f for f in REQUIRED_CLAUDE_FLAGS if not flags[f]]
    if missing:
        result["ok"] = False
        result["error"] = {"code": "AGENT_FLAGS_UNSUPPORTED", "missing": missing}
        return result
    try:
        result["auth"] = check_auth(policy, exe=exe, env=env)
        result["ok"] = True
    except AgentError as exc:
        result["ok"] = False
        result["error"] = {"code": exc.code, "message": exc.message, **exc.details}
    return result


def check_auth(policy: AgentPolicy, *, exe: str | None = None, env: dict[str, str] | None = None) -> dict:
    exe = exe or resolve_exe(policy)
    env = env if env is not None else child_env(policy)
    code, out, err, timed_out = _run([exe, "auth", "status", "--json"], env=env, timeout=30)
    if timed_out:
        raise AgentError("AUTH_STATUS_UNKNOWN", "認証状態の確認がtimeoutしました")
    try:
        status = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AgentError("AUTH_STATUS_UNKNOWN", "認証状態を判定できません。安全のため停止します") from None
    if not status.get("loggedIn"):
        raise AgentError("AUTH_REQUIRED", "Claude にログインしていません。claude を起動してログインしてください")
    method = status.get("authMethod")
    provider = status.get("apiProvider")
    allowed = policy.allowed_auth_methods if policy.auth_mode == "subscription" else ("api_key",)
    if method not in allowed or (provider is not None and provider not in policy.allowed_api_providers):
        raise AgentError("AUTH_MODE_MISMATCH", "実効認証が設定modeと一致しません。API課金への切替を防ぐため停止します",
                         {"auth_method": method, "api_provider": provider, "auth_mode": policy.auth_mode,
                          "allowed": list(allowed)})
    return {"auth_method": method, "api_provider": provider}


def claude_receipt_argv(exe: str, schema_json: str, flags: dict[str, bool], model: str | None) -> list[str]:
    argv = [exe, "-p", "--output-format", "json", "--json-schema", schema_json,
            "--tools", "Read", "--allowedTools", "Read", "--no-session-persistence"]
    if flags.get("--restricted"):
        argv.append("--restricted")
    if flags.get("--strict-mcp-config"):
        argv.append("--strict-mcp-config")
    if flags.get("--disallowedTools"):
        argv += ["--disallowedTools", "mcp__*"]
    if flags.get("--max-turns"):
        argv += ["--max-turns", "4"]
    if model:
        argv += ["--model", model]
    return argv


def parse_claude_wrapper(stdout: bytes, max_bytes: int) -> RunResult:
    if len(stdout) > max_bytes:
        return RunResult("invalid_output", error_code="OUTPUT_TOO_LARGE", error_message="出力が上限を超えました")
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        return RunResult("outcome_unknown", error_code="NO_OUTPUT", error_message="出力がありません")
    try:
        wrapper = json.loads(text)
    except ValueError:
        return RunResult("invalid_output", error_code="WRAPPER_NOT_JSON", error_message="JSON wrapper ではありません")
    if not isinstance(wrapper, dict):
        return RunResult("invalid_output", error_code="WRAPPER_NOT_OBJECT", error_message="JSON wrapper が object ではありません")
    usage = wrapper.get("usage")
    model = None
    if isinstance(wrapper.get("modelUsage"), dict) and wrapper["modelUsage"]:
        model = ",".join(sorted(wrapper["modelUsage"].keys()))
    common = {"wrapper": _redact_wrapper(wrapper), "usage": usage, "model": model}
    if wrapper.get("is_error") or wrapper.get("subtype") not in (None, "success"):
        message = str(wrapper.get("result") or wrapper.get("subtype") or "")[:500]
        lowered = message.lower()
        if any(k in lowered for k in ("usage limit", "rate limit", "quota", "credit balance")):
            return RunResult("quota_wait", error_code="QUOTA_WAIT", error_message=message, **common)
        if any(k in lowered for k in ("log in", "login", "authenticat", "/login")):
            return RunResult("auth_required", error_code="AUTH_REQUIRED", error_message=message, **common)
        return RunResult("failed", error_code=str(wrapper.get("subtype") or "AGENT_ERROR"), error_message=message,
                         **common)
    if "structured_output" not in wrapper or wrapper["structured_output"] is None:
        return RunResult("invalid_output", error_code="STRUCTURED_OUTPUT_MISSING",
                         error_message="structured_output がありません (自然文からは抽出しません)", **common)
    return RunResult("succeeded", structured_output=wrapper["structured_output"], **common)


def _redact_wrapper(wrapper: dict) -> dict:
    keep = ("type", "subtype", "is_error", "duration_ms", "num_turns", "session_id", "total_cost_usd", "usage",
            "modelUsage", "permission_denials")
    return {k: wrapper[k] for k in keep if k in wrapper}


def run_receipt_extract(policy: AgentPolicy, job_dir: Path, file_names: list[str], prompt: str,
                        schema_json: str) -> RunResult:
    """画像抽出jobを1回実行する。認証確認 → 起動 → wrapper解析。採用判断は core が行う。"""
    exe = resolve_exe(policy)
    env = child_env(policy)
    check_auth(policy, exe=exe, env=env)
    _, version_out, _, _ = _run([exe, "--version"], env=env, timeout=30)
    cli_version = version_out.decode("utf-8", "replace").strip()[:100] or None
    _, help_out, _, _ = _run([exe, "--help"], env=env, timeout=30)
    help_text = help_out.decode("utf-8", "replace")
    flags = {f: (f in help_text) for f in REQUIRED_CLAUDE_FLAGS + OPTIONAL_CLAUDE_FLAGS}
    missing = [f for f in REQUIRED_CLAUDE_FLAGS if not flags[f]]
    if missing:
        raise AgentError("AGENT_FLAGS_UNSUPPORTED", "採用CLI版が必要なflagに対応していません", {"missing": missing})
    for name in file_names:
        if "/" in name or "\\" in name or name.startswith("."):
            raise AgentError("INVALID_INPUT", "job入力は内部名のファイルだけです")
    argv = claude_receipt_argv(exe, schema_json, flags, policy.model)
    code, out, err, timed_out = _run(argv, env=env, cwd=job_dir, stdin=prompt, timeout=policy.timeout_seconds,
                                     max_stdout=policy.max_stdout_bytes, max_stderr=policy.max_stderr_bytes)
    stderr_tail = err.decode("utf-8", "replace")[-2000:]
    display_argv = [a if a != schema_json else "<schema>" for a in argv]
    if timed_out:
        partial = parse_claude_wrapper(out, policy.max_stdout_bytes) if out.strip() else None
        if partial is not None and partial.status == "succeeded":
            partial.argv = display_argv
            partial.cli_version = cli_version
            return partial
        return RunResult("timed_out", exit_code=code, stderr_tail=stderr_tail, error_code="TIMEOUT",
                         error_message="timeout。Provider側で処理・利用量が発生した可能性があります", argv=display_argv,
                         cli_version=cli_version)
    result = parse_claude_wrapper(out, policy.max_stdout_bytes)
    result.exit_code = code
    result.stderr_tail = stderr_tail
    result.argv = display_argv
    result.cli_version = cli_version
    if result.status == "outcome_unknown" and code not in (0, None):
        # 出力なしの異常終了は CLI 側のエラー (引数・認証等)。stderr の末尾を記録する
        result.status = "failed"
        result.error_code = "CLI_ERROR"
        result.error_message = (stderr_tail.strip().splitlines() or [f"exit code {code}"])[-1][:500]
    elif result.status == "succeeded" and code != 0:
        result.status = "failed"
        result.error_code = "NONZERO_EXIT"
        result.error_message = f"exit code {code}"
    return result
