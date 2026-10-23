"""core IPC server / client。

Windows の正式経路は named pipe (利用者SID・明示DACL・remote client拒否) だが、Windows実機での検証が
済んでいないため未実装とする。開発・Linux/macOS では利用者だけが読める Unix domain socket (0600) を使う。

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
from typing import Any

from ...domain.values import new_id
from . import framing

PROTOCOL_VERSION = "1.0"


def socket_path(data_dir: Path) -> Path:
    return data_dir / "core.sock"


class NotSupportedOnPlatform(Exception):
    pass


def _require_posix() -> None:
    if sys.platform == "win32":
        raise NotSupportedOnPlatform(
            "Windows named pipe の core server は未実装です (実機でのDACL・remote拒否の検証待ち)。"
            "当面はCLIの in-process 実行を使ってください")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: CoreServer = self.server  # type: ignore[assignment]
        try:
            hello = framing.read_frame(self.rfile)
        except (EOFError, framing.FrameError):
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello" or \
                hello.get("protocol_version") != PROTOCOL_VERSION:
            framing.write_frame(self.wfile, {"type": "error", "code": "PROTOCOL_MISMATCH",
                                             "protocol_version": PROTOCOL_VERSION})
            return
        session = server.session_factory(bool(hello.get("reader")))
        framing.write_frame(self.wfile, {"type": "welcome", "protocol_version": PROTOCOL_VERSION,
                                         "app_version": server.app_version, "profile": session.profile,
                                         "session_id": session.session_id})
        while True:
            try:
                req = framing.read_frame(self.rfile)
            except (EOFError, ConnectionResetError):
                return
            except framing.FrameError as exc:
                framing.write_frame(self.wfile, {"type": "error", "code": "FRAME_ERROR", "message": str(exc)})
                return
            if not isinstance(req, dict) or req.get("type") != "request":
                framing.write_frame(self.wfile, {"type": "error", "code": "INVALID_REQUEST"})
                continue
            resp = server.core.execute(
                str(req.get("command")), req.get("payload") or {}, session,
                request_id=req.get("request_id"), idempotency_key=req.get("idempotency_key"),
                expected_revision=req.get("expected_revision"), dry_run=bool(req.get("dry_run")),
                capability_token=req.get("capability_token"))
            framing.write_frame(self.wfile, {"type": "response", "response": resp})


class CoreServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, core: Any, session_factory, app_version: str):
        _require_posix()
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


class CoreClient:
    def __init__(self, data_dir: Path, *, reader: bool = False, timeout: float | None = None):
        _require_posix()
        path = socket_path(data_dir)
        if not path.exists():
            raise ConnectionError("core が起動していません (asurabook core serve)")
        st = path.stat()
        if st.st_uid != os.getuid():
            raise ConnectionError("socket の所有者が利用者と異なるため接続しません")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rwb")
        framing.write_frame(self.stream, {"type": "hello", "protocol_version": PROTOCOL_VERSION, "reader": reader})
        welcome = framing.read_frame(self.stream)
        if welcome.get("type") != "welcome":
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
        try:
            self.stream.close()
        finally:
            self.sock.close()
