from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
import threading
from typing import Any
import uuid


from dispatcher.runtime.process import ManagedProcess


Command = str | Sequence[str]


def _default_sandbox_root() -> Path:
    # 当前文件位于 src/dispatcher/runtime/containers.py，parents[3] 通常就是项目根目录。
    return Path(__file__).resolve().parents[3] / ".sandboxes"


class ContainerManager:
    """本地沙箱目录管理器。

    这个类故意保留 ContainerManager 这个名字和一组容器版方法名，方便后续复用/迁移
    原来依赖 Docker ContainerManager 的调度代码。这里没有真正创建容器，只是给每个
    project_id 创建一个独立目录，并在该目录中启动 ManagedProcess。
    """

    _PREFIX = "qinyuan-dispatch-"
    _STARTUP_PREFIX = "qinyuan-startup-healthcheck-"
    _STATE_FILE = ".state"

    def __init__(
        self,
        config: Any | None = None,
        *,
        root: str | os.PathLike[str] | None = None,
        completed_action: str | None = None,
    ) -> None:
        config_root = getattr(config, "sandbox_root", None) or getattr(config, "root", None)
        env_root = os.environ.get("QINYUAN_SANDBOX_ROOT")

        # root 的优先级：显式参数 > config > 环境变量 > 项目根目录下 .sandboxes。
        self._root = Path(root or config_root or env_root or _default_sandbox_root()).resolve()
        self._completed_action = completed_action or getattr(config, "completed_action", "remove")
        self._lock = threading.Lock()

    def close(self) -> None:
        """保留和 Docker 版一致的生命周期接口；本地沙箱没有连接需要关闭。"""

    def container_name(self, project_id: str) -> str:
        """把 project_id 转成可作为目录名使用的沙箱名称。"""

        sanitized = "".join(char if char.isalnum() or char in ("-", "_", ".") else "-" for char in project_id)
        sanitized = sanitized.strip("-") or uuid.uuid4().hex[:12].hash()  # 避免空字符串或全非法字符导致目录名冲突。
        return f"{self._PREFIX}{sanitized}"

    def ensure_running(self, project_id: str) -> str:
        """确保项目沙箱目录存在，并标记为 running。"""

        name = self.container_name(project_id)
        with self._lock:
            self._ensure_sandbox(name)
            self._write_state(name, "running")
        return name

    def create_startup_container(self) -> str:
        """创建一次性启动健康检查沙箱。"""

        name = f"{self._STARTUP_PREFIX}{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._ensure_sandbox(name)
            self._write_state(name, "running")
        return name

    def inspect_state(self, name: str) -> str | None:
        """返回沙箱状态；目录不存在时返回 None。"""

        path = self._sandbox_path(name)
        if not path.exists():
            return None
        state_path = path / self._STATE_FILE
        if not state_path.exists():
            return "running"
        state = state_path.read_text(encoding="utf-8").strip()
        return state if state in ("running", "stopped") else "running"

    def cleanup_completed(self, project_id: str) -> bool:
        """项目完成后的清理动作：remove 删除目录，stop 仅标记为 stopped。"""

        name = self.container_name(project_id)
        if self.inspect_state(name) is None:
            return True
        if self._completed_action == "remove":
            self.remove_container(name, force=True)
            return self.inspect_state(name) is None
        self._write_state(name, "stopped")
        return self.inspect_state(name) != "running"

    def cleanup_stopped(self, project_id: str) -> bool:
        """项目被停止时标记沙箱为 stopped。"""

        name = self.container_name(project_id)
        if self.inspect_state(name) is None:
            return True
        self._write_state(name, "stopped")
        return self.inspect_state(name) != "running"

    def cleanup_orphan(self, name: str) -> bool:
        """删除不再属于活跃项目的沙箱目录。"""

        self.remove_container(name, force=True)
        return self.inspect_state(name) is None

    def managed_container_names(self) -> list[str]:
        """列出由当前管理器创建的项目沙箱。"""

        if not self._root.exists():
            return []
        return sorted(path.name for path in self._root.iterdir() if path.is_dir() and path.name.startswith(self._PREFIX))

    def needs_completed_cleanup(self, project_id: str) -> bool:
        name = self.container_name(project_id)
        state = self.inspect_state(name)
        if state is None:
            return False
        if self._completed_action == "remove":
            return True
        return state == "running"

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self.inspect_state(name) is not None

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return self.inspect_state(self.container_name(project_id)) == "running"

    def build_exec_process(
        self,
        container_name: str,
        env: Mapping[str, str],
        command: Command,
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ManagedProcess:
        """在对应沙箱目录中构造一个本地进程。

        kill_after_seconds 保留为兼容参数；本地 ManagedProcess 内部固定使用一个短暂
        grace window 等待进程退出。
        """

        del kill_after_seconds
        sandbox = self._ensure_sandbox(container_name)
        self._write_state(container_name, "running")
        return ManagedProcess(command, dict(env), cwd=sandbox, timeout_seconds=timeout_seconds)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        """把“容器内路径”映射到沙箱目录内，并写入文本文件。"""

        target = self.host_path(container_name, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def host_path(self, container_name: str, path: str) -> Path:
        """把传入的容器风格路径转换成真实宿主机路径。"""

        return self._ensure_sandbox(container_name) / self._relative_container_path(path)

    def remove_container(self, name: str, *, force: bool = True) -> None:
        """删除沙箱目录；force=False 时只标记 stopped，方便保留现场。"""

        path = self._sandbox_path(name)
        if not path.exists():
            return
        if not force:
            self._write_state(name, "stopped")
            return
        shutil.rmtree(path, ignore_errors=True)

    def _ensure_sandbox(self, name: str) -> Path:
        path = self._sandbox_path(name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sandbox_path(self, name: str) -> Path:
        path = (self._root / name).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"invalid sandbox name: {name}") from exc
        return path

    def _write_state(self, name: str, state: str) -> None:
        path = self._ensure_sandbox(name)
        (path / self._STATE_FILE).write_text(state, encoding="utf-8")

    @staticmethod
    def _relative_container_path(path: str) -> Path:
        text = path.strip().replace("\\", "/")
        if not text:
            raise ValueError("container path must not be empty")
        if len(text) >= 2 and text[1] == ":":
            text = text[2:]

        parts = [part for part in text.split("/") if part and part not in (".", "..")]
        if not parts:
            raise ValueError(f"invalid container path: {path}")
        return Path(*parts)
