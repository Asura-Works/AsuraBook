"""core IPC server / client (02章3節)。

- Windows: named pipe。名前に利用者SIDとデータフォルダを含め、DACL は現在の利用者だけ、remote client を拒否し、
  最初のインスタンスだけを作成する (別processによる先取りを検出する)。実装は winpipe.py。
- Linux/macOS (開発用): 利用者だけが読める Unix domain socket (0600)。

接続は handshake で protocol 版を確認する。公開接続の session は assistant (無token) で、
owner の自己申告は受け付けない。capability token は request ごとに渡す。
"""

from __future__ import annotations

import os
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO, Callable

from ...domain.values import new_id
from . import framing

PROTOCOL_VERSION = "1.0"


class NotSupportedOnPlatform(Exception):
    pass


def serve_stream(rfile: BinaryIO, wfile: BinaryIO, core: Any, session_factory: Callable[[bool], Any],
                 app_version: str) -> None:
    """1接続分の handshake と request/response を処理する (transport 共通)。"""
    try:
        hello = framing.read_frame(rfile)
    except (EOFError, OSError, framing.FrameError):
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello" or \
            hello.get("protocol_version") != PROTOCOL_VERSION:
        framing.write_frame(wfile, {"type": "error", "code": "PROTOCOL_MISMATCH", "protocol_version": PROTOCOL_VERSION})
        return
    session = session_factory(bool(hello.get("reader")))
    framing.write_frame(wfile, {"type": "welcome", "protocol_version": PROTOCOL_VERSION, "app_version": app_version,
                                "profile": session.profile, "session_id": session.session_id})
    while True:
        try:
            req = framing.read_frame(rfile)
        except (EOFError, OSError):
            return
        except framing.FrameError as exc:
            framing.write_frame(wfile, {"type": "error", "code": "FRAME_ERROR", "message": str(exc)})
            return
        if not isinstance(req, dict) or req.get("type") != "request":
            framing.write_frame(wfile, {"type": "error", "code": "INVALID_REQUEST"})
            continue
        resp = core.execute(
            str(req.get("command")), req.get("payload") or {}, session,
            request_id=req.get("request_id"), idempotency_key=req.get("idempotency_key"),
            expected_revision=req.get("expected_revision"), dry_run=bool(req.get("dry_run")),
            capability_token=req.get("capability_token"))
        framing.write_frame(wfile, {"type": "response", "response": resp})


# ------------------------------------------------------------------ POSIX (Unix domain socket)

def socket_path(data_dir: Path) -> Path:
    return data_dir / "core.sock"


if hasattr(socket, "AF_UNIX") and sys.platform != "win32":

    class _Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            s: UnixCoreServer = self.server  # type: ignore[assignment]
            serve_stream(self.rfile, self.wfile, s.core, s.session_factory, s.app_version)

    class UnixCoreServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def __init__(self, core: Any, session_factory, app_version: str):
            self.core = core
            self.session_factory = session_factory
            self.app_version = app_version
            path = socket_path(core.paths.root)
            if path.exists():
                if _alive(path):
                    raise RuntimeError(f"core は既に起動しています: {path}")
                path.unlink()  # 応答しない古いsocketだけを掃除する
            old_umask = os.umask(0o177)
            try:
                super().__init__(str(path), _Handler)
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            self.address = str(path)

        def serve_in_thread(self) -> threading.Thread:
            t = threading.Thread(target=self.serve_forever, daemon=True)
            t.start()
            return t

        def server_close(self) -> None:
            super().server_close()
            try:
                os.unlink(self.server_address)  # type: ignore[arg-type]
            except OSError:
                pass


def _alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ------------------------------------------------------------------ factory

def CoreServer(core: Any, session_factory, app_version: str):  # noqa: N802 - 互換のため関数名を class 風にする
    if sys.platform == "win32":
        from .winpipe import PipeCoreServer
        return PipeCoreServer(core, session_factory, app_version)
    if "UnixCoreServer" not in globals():
        raise NotSupportedOnPlatform("この環境では core server を起動できません")
    return UnixCoreServer(core, session_factory, app_version)


class CoreClient:
    def __init__(self, data_dir: Path, *, reader: bool = False, timeout: float | None = None):
        if sys.platform == "win32":
            from .winpipe import connect_pipe
            self.stream, self._closer = connect_pipe(data_dir, timeout)
        else:
            path = socket_path(data_dir)
            if not path.exists():
                raise ConnectionError("core が起動していません (asurabook core serve)")
            if path.stat().st_uid != os.getuid():
                raise ConnectionError("socket の所有者が利用者と異なるため接続しません")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(path))
            self.stream = sock.makefile("rwb")
            stream = self.stream

            def closer() -> None:
                try:
                    stream.close()
                finally:
                    sock.close()
            self._closer = closer
        framing.write_frame(self.stream, {"type": "hello", "protocol_version": PROTOCOL_VERSION, "reader": reader})
        welcome = framing.read_frame(self.stream)
        if welcome.get("type") != "welcome":
            self.close()
            raise ConnectionError(f"handshake に失敗しました: {welcome}")
        self.welcome = welcome

    def execute(self, command: str, payload: dict, **kw: Any) -> dict:
        framing.write_frame(self.stream, {"type": "request", "request_id": kw.get("request_id") or new_id(),
                                          "command": command, "payload": payload,
                                          "idempotency_key": kw.get("idempotency_key"),
                                          "expected_revision": kw.get("expected_revision"),
                                          "dry_run": kw.get("dry_run", False),
                                          "capability_token": kw.get("capability_token")})
        msg = framing.read_frame(self.stream)
        if msg.get("type") != "response":
            raise ConnectionError(f"不正な応答です: {msg}")
        return msg["response"]

    def close(self) -> None:
        self._closer()
