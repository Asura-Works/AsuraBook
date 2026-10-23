"""Windows named pipe transport (02章3節)。pywin32 を使う。

- pipe 名: \\\\.\\pipe\\AsuraBook-<利用者SID>-<データフォルダのhash>
- DACL: 現在の利用者SIDだけに全権 (保護DACL、継承なし)。
- PIPE_REJECT_REMOTE_CLIENTS で remote client を拒否。
- 最初のインスタンスは FILE_FLAG_FIRST_PIPE_INSTANCE で作り、同名pipeを別processが先に作っていたら起動しない。
- client は接続先 pipe の server process の利用者SIDが自分と一致することを確かめてから handshake する。
"""

from __future__ import annotations

import ctypes
import hashlib
import threading
from pathlib import Path
from typing import Any, Callable

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security
import winerror

from .server import serve_stream

FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
BUFFER = 64 * 1024


def current_user_sid() -> Any:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def pipe_name(data_dir: Path) -> str:
    sid = win32security.ConvertSidToStringSid(current_user_sid())
    digest = hashlib.sha256(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\AsuraBook-{sid}-{digest}"


def _security_attributes() -> Any:
    sid = win32security.ConvertSidToStringSid(current_user_sid())
    # D:P = 保護DACL (継承しない)、A;;GA;;;<SID> = 現在の利用者に GENERIC_ALL
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;{sid})", win32security.SDDL_REVISION_1)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = False
    return sa


class PipeStream:
    """framing が使う read/write/flush を持つ pipe handle の wrapper。"""

    def __init__(self, handle: Any):
        self.handle = handle

    def read(self, n: int) -> bytes:
        try:
            _, data = win32file.ReadFile(self.handle, n)
        except pywintypes.error as exc:
            if exc.winerror in (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_PIPE_NOT_CONNECTED):
                return b""
            raise
        return bytes(data)

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        written = 0
        while written < len(data):
            _, n = win32file.WriteFile(self.handle, view[written:].tobytes())
            written += n
        return written

    def flush(self) -> None:
        pass


def server_process_sid(handle: Any) -> Any:
    """接続先 pipe を作った server process の利用者SID。"""
    pid = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetNamedPipeServerProcessId(ctypes.c_void_p(int(handle)), ctypes.byref(pid))
    if not ok:
        raise ConnectionError("pipe の server process を確認できません")
    process = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    try:
        token = win32security.OpenProcessToken(process, win32security.TOKEN_QUERY)
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(process)


def pipe_dacl_sids(handle: Any) -> list[str]:
    """試験・診断用: pipe の DACL に含まれる SID 一覧。"""
    sd = win32security.GetSecurityInfo(handle, win32security.SE_KERNEL_OBJECT,
                                       win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    return [win32security.ConvertSidToStringSid(dacl.GetAce(i)[2]) for i in range(dacl.GetAceCount())]


class PipeCoreServer:
    def __init__(self, core: Any, session_factory: Callable[[bool], Any], app_version: str):
        self.core = core
        self.session_factory = session_factory
        self.app_version = app_version
        self.address = pipe_name(core.paths.root)
        self._sa = _security_attributes()
        self._stop = threading.Event()
        self._first = True
        # 最初のインスタンスをここで作り、同名pipeの先取りを起動時に検出する
        self._pending = self._create()

    def _create(self) -> Any:
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX
        if self._first:
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        try:
            handle = win32pipe.CreateNamedPipe(
                self.address, open_mode,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                win32pipe.PIPE_UNLIMITED_INSTANCES, BUFFER, BUFFER, 0, self._sa)
        except pywintypes.error as exc:
            if self._first and exc.winerror == winerror.ERROR_ACCESS_DENIED:
                raise RuntimeError("同名の pipe が既に存在します (core が起動中か、別processが先に作成しています)") from None
            raise
        self._first = False
        return handle

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            handle = self._pending or self._create()
            self._pending = None
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                if exc.winerror != winerror.ERROR_PIPE_CONNECTED:
                    win32file.CloseHandle(handle)
                    continue
            if self._stop.is_set():
                win32file.CloseHandle(handle)
                break
            threading.Thread(target=self._handle, args=(handle,), daemon=True).start()

    def _handle(self, handle: Any) -> None:
        stream = PipeStream(handle)
        try:
            serve_stream(stream, stream, self.core, self.session_factory, self.app_version)
        finally:
            try:
                win32file.FlushFileBuffers(handle)
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)

    def serve_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self._stop.set()
        # ConnectNamedPipe の待機を解くため自分で一度接続する
        try:
            h = win32file.CreateFile(self.address, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                                     win32con.OPEN_EXISTING, 0, None)
            win32file.CloseHandle(h)
        except pywintypes.error:
            pass

    def server_close(self) -> None:
        if self._pending is not None:
            win32file.CloseHandle(self._pending)
            self._pending = None


def connect_pipe(data_dir: Path, timeout: float | None) -> tuple[PipeStream, Callable[[], None]]:
    name = pipe_name(data_dir)
    try:
        if timeout:
            win32pipe.WaitNamedPipe(name, int(timeout * 1000))
        handle = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                                      win32con.OPEN_EXISTING, 0, None)
    except pywintypes.error as exc:
        if exc.winerror in (winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_SEM_TIMEOUT):
            raise ConnectionError("core が起動していません (asurabook core serve)") from None
        raise ConnectionError(f"core に接続できません ({exc.strerror})") from None
    try:
        same_user = server_process_sid(handle) == current_user_sid()
    except Exception:
        same_user = False
    if not same_user:
        win32file.CloseHandle(handle)
        raise ConnectionError("pipe の server が同じ利用者の process と確認できないため接続しません")
    win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE, None, None)
    return PipeStream(handle), lambda: win32file.CloseHandle(handle)
