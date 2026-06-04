from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from server.models import ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    """协议层错误，保留 HTTP 状态码和响应文本，便于上层记录详细失败原因。"""

    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    """非查询类接口的统一结果。

    查询类接口会直接返回 Pydantic 模型；认领、心跳、释放、提交结论这类接口使用
    ApiResult，让调度器可以根据 status_code 判断是成功、冲突、权限失败还是网络错误。
    """

    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        """HTTP 2xx 都视为成功。"""

        return 200 <= self.status_code < 300


class QinyuanClient:
    """钦原 dispatcher 访问 server 的 HTTP 客户端。

    读接口直接 raise_for_status 并返回模型，
    写接口返回 ApiResult，方便调度循环按状态码做重试、释放或跳过。
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        # list[ProjectSummary] 这种泛型模型需要 TypeAdapter 来做 Pydantic v2 校验。
        self._summary_adapter = TypeAdapter(list[ProjectSummary])

        # requests.Session 不是严格线程安全的，所以每个线程懒创建一个 session。
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        """关闭所有线程里创建过的 HTTP session。"""

        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        """获取项目列表。"""

        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        """获取单个项目的完整图数据。"""

        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        """读取 server 当前调度相关设置。"""

        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        """以 YAML 文本导出项目。"""

        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def create_project(
        self,
        title: str,
        origin: str,
        goal: str,
        hints: list[dict[str, str]] | None = None,
    ) -> ApiResult:
        """创建项目；hints 的元素形如 {"content": "...", "creator": "..." }。"""

        payload: dict[str, Any] = {"title": title, "origin": origin, "goal": goal}
        if hints is not None:
            payload["hints"] = hints
        return self._request_json("POST", "/projects", json=payload)

    def create_hint(self, project_id: str, content: str, creator: str) -> ApiResult:
        """向项目追加人工 hint。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/hints",
            json={"content": content, "creator": creator},
        )

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        """给某个 intent 续约，表示 worker 仍在处理它。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        """认领项目级 reason lease。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        """给项目级 reason lease 续约。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        """释放项目级 reason lease。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        """释放某个 intent 的 worker 占用。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        """结束 intent，并提交由它产生的 fact 描述。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        """声明整个项目已经完成。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        *,
        worker: str | None = None,
        kind: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ApiResult:
        """创建新的探索 intent；可选 worker 表示创建时顺便认领。"""

        payload: dict[str, Any] = {
            "from": from_ids,
            "description": description,
            "creator": creator,
            "worker": worker,
        }
        if kind is not None:
            payload["kind"] = kind
        if params is not None:
            payload["params"] = params
        return self._request_json("POST", f"/projects/{project_id}/intents", json=payload)

    def update_project_status(self, project_id: str, status: str) -> ApiResult:
        """在 active/stopped 之间切换项目状态。"""

        return self._request_json("PATCH", f"/projects/{project_id}/status", json={"status": status})

    def update_project_title(self, project_id: str, title: str) -> ApiResult:
        """修改项目标题。"""

        return self._request_json("PATCH", f"/projects/{project_id}/title", json={"title": title})

    def reopen_project(self, project_id: str, description: str, creator: str) -> ApiResult:
        """重新打开已完成项目，并提交纠错说明。"""

        return self._request_json(
            "POST",
            f"/projects/{project_id}/reopen",
            json={"description": description, "creator": creator},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        """发送 JSON 请求，并把网络错误也折叠成 ApiResult。"""

        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))

        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        """拼接完整 URL，调用方传入的 path 应以 / 开头。"""

        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        """返回当前线程专属 session，并配置连接池。"""

        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session

        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session

