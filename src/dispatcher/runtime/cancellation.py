from __future__ import annotations

import threading

from dispatcher.runtime.process import ManagedProcess


class TaskCancellation:
    """线程安全的任务取消令牌，用来把调度层的取消请求传递给正在运行的进程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: ManagedProcess | None = None

        # _reason 一旦被设置，就表示任务已经取消；后续重复 cancel 不会覆盖第一次原因。
        self._reason: str | None = None

    def attach_process(self, process: ManagedProcess | None) -> None:
        """绑定当前正在执行的进程；如果任务已经取消，绑定后立刻取消该进程。"""

        with self._lock:
            self._process = process
            reason = self._reason

        # 在锁外调用 process.cancel，避免进程终止逻辑反过来阻塞取消令牌的锁。
        if process is not None and reason is not None:
            process.cancel(reason)

    def cancel(self, reason: str) -> bool:
        """请求取消任务；首次取消返回 True，重复取消返回 False。"""

        with self._lock:
            already_cancelled = self._reason is not None
            if not already_cancelled:
                self._reason = reason
            process = self._process

        # 即使是重复取消，也尝试通知当前进程；ManagedProcess 内部会保持第一次原因。
        if process is not None:
            process.cancel(reason)
        return not already_cancelled

    @property
    def is_cancelled(self) -> bool:
        """当前任务是否已经收到取消请求。"""

        return self.reason is not None

    @property
    def reason(self) -> str | None:
        """返回首次取消原因；未取消时返回 None。"""

        with self._lock:
            return self._reason
