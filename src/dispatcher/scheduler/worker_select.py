from __future__ import annotations

from dataclasses import dataclass
import random


@dataclass(slots=True)
class WorkerCandidate:
    """调度器内部的 worker 候选项。

    钦原当前配置是单 worker，但这里保留候选项结构，后续扩展成多 worker 时不用重写
    调度排序逻辑。
    """

    name: str
    max_running: int
    priority: int = 0


def choose_worker(candidates: list[WorkerCandidate], running_counts: dict[str, int]) -> list[WorkerCandidate]:
    """按优先级、当前运行数和随机扰动排序 worker。

    priority 越小越优先；运行数越少越优先；最后用 random 打散同级候选，避免长期固定顺序。
    """

    return sorted(
        candidates,
        key=lambda worker: (
            worker.priority,
            running_counts.get(worker.name, 0),
            random.random(),
        ),
    )
