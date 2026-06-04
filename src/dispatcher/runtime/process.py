from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading

LOG = logging.getLogger(__name__)
EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0


# 支持两种命令形式：
# - list/tuple：按参数数组直接执行，适合确定的程序调用。
# - str：默认走 shell=True，适合调用方已经拼好的命令字符串。
Command = str | Sequence[str]


@dataclass(slots=True)
class ProcessResult:
    """子进程执行结果，包含退出码、标准输出和标准错误输出。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


class ManagedProcess:
    """受管理的本地子进程，用于不考虑强隔离时的 Python/本机沙箱执行。"""

    def __init__(
        self,
        command: Command,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        *,
        timeout_seconds: float | None = None,
        shell: bool | None = None,
    ) -> None:
        self.command = command

        # 默认继承当前进程环境，再叠加调用方传入的变量，避免 PATH 等基础变量丢失。
        self.env = dict(os.environ)
        if env is not None:
            self.env.update({str(key): str(value) for key, value in env.items()})

        # cwd 作为本地“沙箱目录”使用；启动前会自动创建。
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.timeout_seconds = timeout_seconds

        # shell 未显式指定时，字符串命令走 shell，参数数组不走 shell。
        self.shell = isinstance(command, str) if shell is None else shell

        self._process: subprocess.Popen[str] | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.cwd is not None:
            self.cwd.mkdir(parents=True, exist_ok=True)

        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            # Windows 下创建新的进程组，后续 taskkill /T 可以带走子进程树。
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Unix 下创建新的 session，后续 os.killpg 可以杀整个进程组。
            start_new_session = True

        with self._lock:
            if self._process is not None:
                raise RuntimeError("process has already been started")
            self._process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=self.shell,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
            should_cancel = self._cancel_reason is not None

        # 如果取消请求早于 start() 到达，启动后立刻终止。
        if should_cancel:
            self.kill()

    def communicate(self, timeout: float | None = None) -> ProcessResult:
        process = self._require_process()

        # 构造函数里的 timeout_seconds 是任务级超时，优先级高于 communicate 的临时等待值。
        effective_timeout = self.timeout_seconds if self.timeout_seconds is not None else timeout
        stdout = ""
        stderr = ""

        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired as exc:
            self._timed_out = True
            self.kill()
            # kill 后再短暂等待一次，尽量把已经写出的 stdout/stderr 收完整。
            stdout, stderr = self._communicate_after_kill(process, exc)

        return ProcessResult(
            returncode=self._returncode(process),
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return

        # 按平台清理整个进程树，避免只杀父进程后留下子进程。
        if os.name == "nt":
            self._kill_windows(process)
        else:
            self._kill_posix(process)

    def cancel(self, reason: str) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is not None:
                return
            if self._cancel_reason is None:
                self._cancel_reason = reason
        self.kill()

    def _communicate_after_kill(
        self,
        process: subprocess.Popen[str],
        exc: subprocess.TimeoutExpired,
    ) -> tuple[str, str]:
        try:
            return process.communicate(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as second_exc:
            LOG.warning("process did not exit after kill pid=%s", process.pid)
            return self._decode(second_exc.output or exc.output), self._decode(second_exc.stderr or exc.stderr)

    def _kill_windows(self, process: subprocess.Popen[str]) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            LOG.warning("failed to taskkill process pid=%s error=%s", process.pid, exc)
        if process.poll() is None:
            # taskkill 在某些环境下可能失败或延迟，直接 kill 父进程做兜底。
            process.kill()

    def _kill_posix(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            LOG.warning("failed to kill process group pid=%s error=%s", process.pid, exc)
            process.kill()

    def _returncode(self, process: subprocess.Popen[str]) -> int:
        returncode = process.returncode

        # 约定 137 表示“被强制终止”，和容器/Unix SIGKILL 的常见语义保持接近。
        if self._timed_out:
            return 137
        if self._cancel_reason is not None and (returncode is None or returncode != 0):
            return 137
        if returncode is None:
            return 1
        return int(returncode)

    def _require_process(self) -> subprocess.Popen[str]:
        with self._lock:
            process = self._process
        if process is None:
            raise RuntimeError("process has not been started")
        return process

    @staticmethod
    def _decode(value: bytes | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
