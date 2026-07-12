import json
import threading
from pathlib import Path


class LayoutConflict(RuntimeError):
    def __init__(self, current_revision: int) -> None:
        super().__init__(f"layout revision is {current_revision}")
        self.current_revision = current_revision


class LayoutStore:
    def __init__(self, project_dir: Path) -> None:
        self.path = project_dir / "layout.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _default() -> dict[str, object]:
        return {
            "revision": 0,
            "nodes": {},
            "edges": {},
            "viewport": {},
        }

    def load(self) -> dict[str, object]:
        if not self.path.exists():
            return self._default()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("layout must be an object")
        return payload

    def save(
        self,
        expected_revision: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        with self._lock:
            current = self.load()
            current_revision = int(current["revision"])
            if current_revision != expected_revision:
                raise LayoutConflict(current_revision)
            next_value = {
                "revision": expected_revision + 1,
                "nodes": payload.get("nodes", {}),
                "edges": payload.get("edges", {}),
                "viewport": payload.get("viewport", {}),
            }
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(next_value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
            return next_value
