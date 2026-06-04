from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from dispatcher.protocol.client import ApiResult,QinyuanClient
from dispatcher.runtime.process import ManagedProcess

LOG = logging.getLogger(__name__)
HEARTBEAT_FAILURE_GRACE_MULTIPLIER = 2

@dataclass(slots=True)
class HeartbeatFailure:
    """心跳失败信息，包含状态码和响应文本。"""
    status_code: int|None
    text:str

class HeartbeatLease:
    """心跳租约，包含上次成功心跳的时间戳和失败信息（如果有）。"""

    def __init__(
            self,
            heartbeat:Callable[[], ApiResult],
            scope:str,
            worker_name:str,
            interval:int,
    ):
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        self._process: ManagedProcess | None = None
        self._failure: HeartbeatFailure | None = None
        self._last_success_at=time.monotonic()
        self._stop=threading.Event()
        self._lock=threading.Lock()
        self._thread=threading.Thread(target=self._run,daemon=True)

    @classmethod
    def for_intent(
        cls,
        client: QinyuanClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        interval: int,
    )->"HeartbeatLease":
        """根据项目意图创建一个心跳租约，心跳函数会调用 client.heartbeat 接口，并传入项目 ID、意图 ID 和工作线程名称。"""
        return cls(
            heartbeat=lambda:client.heartbeat(project_id,intent_id,worker_name),
            scope=f"project:{project_id}:intent:{intent_id}",
            worker_name=worker_name,
            interval=interval,
        )
    @classmethod
    def for_reason(
        cls,
        client: QinyuanClient,
        project_id: str,
        worker_name: str,
        interval: int,
    )->"HeartbeatLease":
        """根据项目创建 reason 心跳租约，心跳函数会调用项目级 reason heartbeat 接口。"""
        return cls(
            heartbeat=lambda:client.reason_heartbeat(project_id,worker_name),
            scope=f"project:{project_id}:reason",
            worker_name=worker_name,
            interval=interval,
        )

    def start(self)-> None:
        """启动心跳线程。"""

        self._thread.start()
    def stop(self)-> None:
        """停止心跳线程，并等待其退出。"""
        self._stop.set()
        self._thread.join(timeout=1)
    def attach_process(self,process:ManagedProcess|None)-> None:
        """绑定当前正在执行的进程；心跳线程会在检测到租约失效时取消该进程。"""
        with self._lock:
            self._process=process
    
    @property
    def failure(self):
        return self._failure
    def _run(self)-> None:
        """心跳线程的主循环，定期调用心跳函数，并根据结果更新租约状态。"""
        while not self._stop.wait(self._interval):
            result=self._heartbeat()
            if result.ok:
                self._last_success_at=time.monotonic()
                continue
            if result.status_code in (403,409):
                self._fail(result.status_code,result.text)
                return
            elapsed=time.monotonic()-self._last_success_at
            grace_seconds=max(float(self._interval),float(self._interval)*HEARTBEAT_FAILURE_GRACE_MULTIPLIER)
            LOG.warning(
                "heartbeat transient failure scope=%s worker=%s status=%s elapsed=%.1fs grace=%.1fs",
                self._scope,
                self._worker_name,
                result.status_code,
                elapsed,
                grace_seconds,
            )
            if elapsed < grace_seconds:
                continue
            self._fail(result.status_code or None, result.text)
            return

    def _fail(self, status_code: int | None, text: str) -> None:
        self._failure = HeartbeatFailure(status_code, text)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        with self._lock:
            process = self._process
        if process is not None:
            process.kill()
