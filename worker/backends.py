from worker.backend import ExecutionBackend
from worker.container_backend import ContainerExecutionBackend
from worker.executor import LocalExecutionBackend
from worker.models import ExecutionConfig


def build_execution_backend(config: ExecutionConfig) -> ExecutionBackend:
    if config.backend == "local":
        return LocalExecutionBackend()
    if config.backend == "container":
        if config.container is None:
            raise ValueError("container backend configuration is required")
        return ContainerExecutionBackend(config.container)
    raise ValueError(f"unsupported execution backend: {config.backend}")
