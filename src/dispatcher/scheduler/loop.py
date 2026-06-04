from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os
import time
from typing import Literal

import requests

from dispatcher.config import DispatcherConfig, TaskType
from dispatcher.protocol.client import QinyuanClient
from dispatcher.runtime.cancellation import TaskCancellation
from dispatcher.runtime.containers import ContainerManager
from dispatcher.runtime.heartbeat import HeartbeatLease
from dispatcher.scheduler.worker_select import WorkerCandidate, choose_worker
from server.models import Intent, ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_INTERVAL_SECONDS = 5.0
UNHEALTHY_RETRY_AFTER_SECONDS = 5.0
REJECTED_RETRY_AFTER_SECONDS = 5.0
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"

TaskOutcome = Literal["success", "failed", "cancelled", "noop", "rejected", "unhealthy"]


@dataclass(slots=True)
class ReasonCheckpoint:
    """reason 调度检查点，用来避免图状态未变化时重复触发 reason。"""

    fact_count: int
    hint_count: int
    open_intent_count: int


@dataclass(slots=True)
class RunningTask:
    """本地 executor 中正在执行的调度任务。"""

    project_id: str
    task_type: TaskType
    worker_name: str
    cancellation: TaskCancellation
    intent_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None


@dataclass(slots=True)
class WorkerSelection:
    worker_name: str | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_disabled: list[str]
    blocked_no_handler: list[str]


@dataclass(slots=True)
class TaskContext:
    """传给具体任务 handler 的上下文。

    scheduler 只负责认领、心跳、取消和并发控制；真正的 LLM/工具调用逻辑由 handler 实现。
    """

    config: DispatcherConfig
    client: QinyuanClient
    container_manager: ContainerManager
    project: ProjectDetail
    task_type: TaskType
    worker_name: str
    cancellation: TaskCancellation
    heartbeat: HeartbeatLease
    intent: Intent | None = None
    export_yaml: str | None = None
    reason_trigger: str | None = None


TaskHandler = Callable[[TaskContext], TaskOutcome | str | None]


class DispatcherLoop:
    """钦原调度主循环。

    这版调度器按钦原当前模型写成单 worker 架构：从 server 拉项目，按 bootstrap/explore/reason
    顺序挑任务，认领成功后把 TaskContext 交给外部注入的 handler。
    """

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        server_url: str | None = None,
        handlers: Mapping[str, TaskHandler] | None = None,
        client: QinyuanClient | None = None,
        container_manager: ContainerManager | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.interval_seconds = interval_seconds or _env_float("QINYUAN_DISPATCH_INTERVAL", DEFAULT_INTERVAL_SECONDS)
        self.handlers = dict(handlers or {})

        self.client = client or QinyuanClient(server_url or os.environ.get("QINYUAN_SERVER", DEFAULT_SERVER_URL))
        self._owns_client = client is None
        self.container_manager = container_manager or ContainerManager(config, completed_action=config.completed_action)
        self._owns_container_manager = container_manager is None

        self.executor = ThreadPoolExecutor(max_workers=config.max_concurrent_tasks)
        cleanup_workers = max(1, min(4, config.max_concurrent_tasks))
        self.cleanup_executor = ThreadPoolExecutor(max_workers=cleanup_workers)

        self.futures: dict[Future[TaskOutcome | str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._settings_checked = False
        self._project_cursor = 0

    def close(self) -> None:
        """关闭 executor、沙箱管理器和 HTTP 客户端。"""

        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        if self._owns_container_manager:
            self.container_manager.close()
        if self._owns_client:
            self.client.close()

    def run(self, *, once: bool = False) -> None:
        """启动调度循环；once=True 时只跑一轮。"""

        try:
            while True:
                try:
                    self.tick()
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning("dispatcher request failed error=%s retry_in=%.1fs", exc, self.interval_seconds)
                if once:
                    return
                time.sleep(self.interval_seconds)
        finally:
            self.close()

    def tick(self) -> None:
        """执行一次调度轮询。"""

        if not self._settings_checked:
            self._validate_server_settings()
            self._settings_checked = True
        self._reap_futures()
        self._reap_cleanup_futures()
        summaries = self.client.list_projects()
        self._initialize_reason_checkpoints(summaries)
        self._cancel_inactive_tasks(summaries)
        self._queue_sandbox_cleanups(summaries)
        self._dispatch_available(summaries)

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.max_concurrent_tasks:
            self._log_changed(
                "dispatch:global:max_concurrent",
                logging.INFO,
                "skip dispatch because max_concurrent_tasks reached running_tasks=%s",
                len(self.futures),
            )
            return

        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch:global:no_active", logging.INFO, "skip dispatch because no active projects")
            return

        for summary in self._ordered_projects(active):
            if len(self.futures) >= self.config.max_concurrent_tasks:
                return
            if self._project_running_task_count(summary.id) > 0:
                continue
            self._try_dispatch_project(summary)

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"project:{summary.id}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because sandbox cleanup is pending",
                summary.id,
            )
            return False

        project = self.client.get_project(summary.id)
        if project.project.status != "active":
            return False

        if self._is_initial_project(project):
            return self._dispatch_initial_project(project)
        if self._dispatch_unclaimed_intent(project):
            return True
        return self._dispatch_reason_if_needed(project)

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        if not self._task_can_run(project.project.id, "bootstrap"):
            return False

        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if intent.worker is not None:
            return False
        return self._claim_and_submit(project, "bootstrap", intent=intent)

    def _dispatch_unclaimed_intent(self, project: ProjectDetail) -> bool:
        if not self._task_can_run(project.project.id, "explore"):
            return False

        running_intents = self._project_running_intent_ids(project.project.id)
        unclaimed = [
            intent
            for intent in project.intents
            if intent.to is None
            and intent.worker is None
            and intent.id not in running_intents
            and not self._is_bootstrap_intent(intent)
        ]
        if not unclaimed:
            return False
        newest = max(unclaimed, key=lambda intent: intent.created_at)
        return self._claim_and_submit(project, "explore", intent=newest)

    def _dispatch_reason_if_needed(self, project: ProjectDetail) -> bool:
        if not self._task_can_run(project.project.id, "reason"):
            return False
        if project.project.reason is not None:
            return False

        trigger = self._reason_trigger(project)
        if trigger is None:
            return False
        return self._claim_and_submit(project, "reason", reason_trigger=trigger)

    def _claim_and_submit(
        self,
        project: ProjectDetail,
        task_type: TaskType,
        *,
        intent: Intent | None = None,
        reason_trigger: str | None = None,
    ) -> bool:
        selection = self._select_worker(project.project.id, task_type)
        if selection.worker_name is None:
            LOG.info(
                "no worker available project=%s task=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s disabled=%s no_handler=%s",
                project.project.id,
                task_type,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
                selection.blocked_task_disabled,
                selection.blocked_no_handler,
            )
            return False
        worker_name = selection.worker_name

        # 导出 YAML 是给任务 handler 的辅助上下文；如果 server 暂未实现导出接口，
        # 调度本身仍然可以继续，handler 可按需使用 project 里的结构化数据。
        export_yaml = self._safe_export_project(project.project.id)
        heartbeat_interval = max(1, int(self.interval_seconds))

        if task_type == "reason":
            claim = self.client.claim_reason(project.project.id, worker_name, reason_trigger or "change")
            lease = HeartbeatLease.for_reason(self.client, project.project.id, worker_name, heartbeat_interval)
        else:
            if intent is None:
                raise ValueError(f"{task_type} task requires an intent")
            claim = self.client.heartbeat(project.project.id, intent.id, worker_name)
            lease = HeartbeatLease.for_intent(self.client, project.project.id, intent.id, worker_name, heartbeat_interval)

        if claim.status_code in (403, 409):
            LOG.info("claim skipped project=%s task=%s worker=%s status=%s", project.project.id, task_type, worker_name, claim.status_code)
            return False
        if not claim.ok:
            LOG.warning("claim failed project=%s task=%s worker=%s status=%s body=%s", project.project.id, task_type, worker_name, claim.status_code, claim.text)
            return False

        cancellation = TaskCancellation()
        context = TaskContext(
            config=self.config,
            client=self.client,
            container_manager=self.container_manager,
            project=project,
            task_type=task_type,
            worker_name=worker_name,
            cancellation=cancellation,
            heartbeat=lease,
            intent=intent,
            export_yaml=export_yaml,
            reason_trigger=reason_trigger,
        )
        handler = self.handlers[task_type]
        future = self.executor.submit(self._run_handler, handler, context)
        self.futures[future] = RunningTask(
            project_id=project.project.id,
            task_type=task_type,
            worker_name=worker_name,
            cancellation=cancellation,
            intent_id=intent.id if intent is not None else None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
        )
        LOG.info("dispatched task project=%s task=%s worker=%s intent=%s", project.project.id, task_type, worker_name, intent.id if intent else "-")
        return True

    def _run_handler(self, handler: TaskHandler, context: TaskContext) -> TaskOutcome | str:
        context.heartbeat.start()
        try:
            outcome = handler(context) or "success"
            if context.cancellation.is_cancelled:
                return "cancelled"
            if context.heartbeat.failure is not None:
                return "unhealthy"
            return outcome
        except Exception:
            LOG.exception("task handler crashed project=%s task=%s", context.project.project.id, context.task_type)
            return "failed"
        finally:
            context.heartbeat.stop()

    def _select_worker(self, project_id: str, task_type: TaskType) -> WorkerSelection:
        now = time.time()
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_disabled: list[str] = []
        blocked_no_handler: list[str] = []

        if not self._task_enabled(task_type):
            blocked_task_disabled.append(task_type)
            return WorkerSelection(None, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)
        if task_type not in self.handlers:
            blocked_no_handler.append(task_type)
            return WorkerSelection(None, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)

        worker = WorkerCandidate(self.config.worker, self.config.max_concurrent_tasks)
        running = self._worker_counts().get(worker.name, 0)
        if running >= worker.max_running:
            blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
            return WorkerSelection(None, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)

        unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
        if unhealthy_until > now:
            blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
            return WorkerSelection(None, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)

        rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
        if rejected_until > now:
            blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
            return WorkerSelection(None, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)

        ordered = choose_worker([worker], self._worker_counts())
        return WorkerSelection(ordered[0].name, blocked_busy, blocked_unhealthy, blocked_rejected, blocked_task_disabled, blocked_no_handler)

    def _task_can_run(self, project_id: str, task_type: TaskType) -> bool:
        return self._select_worker(project_id, task_type).worker_name is not None

    def _task_enabled(self, task_type: TaskType) -> bool:
        for task in self.config.tasks:
            if task.task_type == task_type:
                return task.enabled
        return False

    def _safe_export_project(self, project_id: str) -> str | None:
        try:
            return self.client.export_project(project_id)
        except requests.RequestException as exc:
            LOG.warning("project export failed project=%s error=%s", project_id, exc)
            return None

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            outcome = future.result()
            if outcome == "success":
                LOG.info("task succeeded project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)
                if task.task_type == "reason":
                    self._update_reason_checkpoint(task)
            else:
                LOG.warning("task finished project=%s task=%s worker=%s outcome=%s", task.project_id, task.task_type, task.worker_name, outcome)
                self._release_claim(task)

            now = time.time()
            if outcome == "unhealthy":
                self.worker_unhealthy_until[task.worker_name] = now + UNHEALTHY_RETRY_AFTER_SECONDS
            else:
                self.worker_unhealthy_until.pop(task.worker_name, None)

            rejection_key = (task.project_id, task.task_type, task.worker_name)
            if outcome == "rejected":
                self.worker_rejected_until[rejection_key] = now + REJECTED_RETRY_AFTER_SECONDS
            else:
                self.worker_rejected_until.pop(rejection_key, None)

    def _release_claim(self, task: RunningTask) -> None:
        if task.task_type == "reason":
            response = self.client.release_reason(task.project_id, task.worker_name)
        elif task.intent_id is not None:
            response = self.client.release(task.project_id, task.intent_id, task.worker_name)
        else:
            return
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s task=%s worker=%s status=%s", task.project_id, task.task_type, task.worker_name, response.status_code)

    def _update_reason_checkpoint(self, task: RunningTask) -> None:
        if task.fact_count is None or task.hint_count is None or task.open_intent_count is None:
            return
        self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
            fact_count=task.fact_count,
            hint_count=task.hint_count,
            open_intent_count=task.open_intent_count,
        )

    def _queue_sandbox_cleanups(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status not in ("completed", "stopped"):
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue

            if summary.status == "completed":
                needs_cleanup = self.container_manager.needs_completed_cleanup(summary.id)
                cleanup = self.container_manager.cleanup_completed
            else:
                needs_cleanup = self.container_manager.needs_stopped_cleanup(summary.id)
                cleanup = self.container_manager.cleanup_stopped
            if not needs_cleanup:
                self._inactive_cleanup_done[summary.id] = summary.status
                continue

            future = self.cleanup_executor.submit(cleanup, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                if future.result() and project_id is not None and status is not None:
                    self._inactive_cleanup_done[project_id] = status
            except Exception:
                LOG.exception("sandbox cleanup failed sandbox=%s", name)
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info("cancelling task for inactive project project=%s task=%s status=%s", task.project_id, task.task_type, status)

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active" or summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"

        open_intent_count = self._project_open_intent_count(project)
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        return ",".join(changes) if changes else None

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if not response.ok:
            LOG.warning("bootstrap intent create failed project=%s status=%s body=%s", project_id, response.status_code, response.text)
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned no JSON object project=%s", project_id)
            return None
        return Intent.model_validate(response.data)

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        by_id = {summary.id: summary for summary in summaries}
        ids = sorted(by_id)
        offset = self._project_cursor % len(ids)
        self._project_cursor += 1
        return [by_id[project_id] for project_id in ids[offset:] + ids[:offset]]

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_intent_ids(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.intent_id is not None
        }

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if intent.to is None)

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= self.interval_seconds:
                raise RuntimeError(f"server {name}={value}s must be greater than dispatcher interval={self.interval_seconds}s")
            if value < self.interval_seconds * 2:
                LOG.warning("server %s is tight value=%ss interval=%.1fs", name, value, self.interval_seconds)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("invalid float env %s=%r; using default %.1f", name, raw, default)
        return default
    return value if value > 0 else default
