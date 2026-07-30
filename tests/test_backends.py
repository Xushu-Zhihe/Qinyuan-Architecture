import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.backends import build_execution_backend
from worker.container_backend import ContainerExecutionBackend
from worker.executor import LocalExecutionBackend
from worker.models import (
    ContainerBackendConfig,
    ExecutionConfig,
    LogChunk,
    ProviderConfig,
    WorkerResult,
)


FAKE_DOCKER = Path(__file__).parent / "fixtures" / "fake_docker.py"


def provider(
    command: tuple[str, ...],
    *,
    timeout: float = 5.0,
    image: str | None = "fake:image",
) -> ProviderConfig:
    return ProviderConfig(
        name="fake",
        enabled=True,
        priority=10,
        command=command,
        timeout_seconds=timeout,
        container_image=image,
    )


class BackendConstructionTests(unittest.TestCase):
    def test_builds_configured_backends(self) -> None:
        self.assertIsInstance(
            build_execution_backend(ExecutionConfig(backend="local")),
            LocalExecutionBackend,
        )
        self.assertIsInstance(
            build_execution_backend(
                ExecutionConfig(
                    backend="container",
                    container=ContainerBackendConfig(default_image="fake:image"),
                )
            ),
            ContainerExecutionBackend,
        )

    def test_container_arguments_enforce_explicit_boundaries(self) -> None:
        backend = ContainerExecutionBackend(
            ContainerBackendConfig(
                default_image="default:image",
                network="none",
                capabilities=("NET_BIND_SERVICE",),
                environment=(("LITERAL_ONLY", "configured"),),
                environment_allowlist=("ALLOWED_ONLY",),
                cleanup_policy="remove",
                memory_limit="256m",
                cpu_limit=0.5,
                pids_limit=32,
            )
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "ALLOWED_ONLY": "allowed",
                "HOST_SECRET_SHOULD_NOT_PASS": "secret",
            },
            clear=False,
        ):
            root = Path(directory)
            challenge = root / "challenge"
            work = root / "work"
            challenge.mkdir()
            work.mkdir()
            arguments = backend._create_arguments(
                provider(("agent", "{challenge_dir}", "{work_dir}")),
                challenge,
                work,
                ("agent", "/challenge", "/work"),
                "qinyuan-test",
            )

        joined = "\n".join(arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn("--network\nnone", joined)
        self.assertIn("target=/challenge,readonly", joined)
        self.assertIn("target=/work", joined)
        self.assertNotIn("target=/work,readonly", joined)
        self.assertIn("--cap-add\nNET_BIND_SERVICE", joined)
        self.assertIn("--env\nALLOWED_ONLY=allowed", joined)
        self.assertIn("--env\nLITERAL_ONLY=configured", joined)
        self.assertNotIn("HOST_SECRET_SHOULD_NOT_PASS", joined)
        self.assertIn("--memory\n256m", joined)
        self.assertIn("--cpus\n0.5", joined)
        self.assertIn("--pids-limit\n32", joined)


class BackendExecutionTests(unittest.IsolatedAsyncioTestCase):
    def fake_backend(
        self,
        state_dir: Path,
        *,
        cleanup: str = "remove",
    ) -> ContainerExecutionBackend:
        return ContainerExecutionBackend(
            ContainerBackendConfig(
                runtime_command=(sys.executable, str(FAKE_DOCKER), str(state_dir)),
                default_image="fake:image",
                cleanup_policy=cleanup,
                read_only_root=True,
                setup_timeout_seconds=5.0,
                stop_timeout_seconds=0.1,
            )
        )

    async def test_local_and_container_return_worker_results_and_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_chunks: list[LogChunk] = []
            local = await LocalExecutionBackend().execute(
                provider(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print(sys.stdin.read(), end='')",
                    ),
                    image=None,
                ),
                "local-request",
                None,
                root / "local",
                on_chunk=local_chunks.append,
            )
            container_chunks: list[LogChunk] = []
            container = await self.fake_backend(root / "state").execute(
                provider(("fake-echo",)),
                "container-request",
                None,
                root / "container",
                on_chunk=container_chunks.append,
            )

        self.assertIsInstance(local, WorkerResult)
        self.assertIsInstance(container, WorkerResult)
        self.assertEqual(local.status, "success")
        self.assertEqual(local.output, "local-request")
        self.assertEqual(local.execution_backend, "local")
        self.assertEqual(local.isolation, "unsafe_local")
        self.assertEqual(local.backend_metadata["challenge_enforcement"], "prompt_only")
        self.assertEqual(container.status, "success")
        self.assertEqual(container.output, "container-request")
        self.assertEqual(container.execution_backend, "container")
        self.assertEqual(container.isolation, "container")
        self.assertEqual(container.cleanup_status, "removed")
        self.assertTrue(any(chunk.stream == "stdout" for chunk in local_chunks))
        self.assertTrue(any(chunk.stream == "stdout" for chunk in container_chunks))

    async def test_cleanup_policies_record_actual_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {"keep": "kept", "stop": "stopped", "remove": "removed"}
            for policy, status in expected.items():
                with self.subTest(policy=policy):
                    result = await self.fake_backend(
                        root / f"state-{policy}",
                        cleanup=policy,
                    ).execute(
                        provider(("fake-echo",)),
                        "ok",
                        None,
                        root / f"work-{policy}",
                    )
                    self.assertEqual(result.cleanup_policy, policy)
                    self.assertEqual(result.cleanup_status, status)
                    assert result.backend_run_id is not None
                    state = root / f"state-{policy}" / f"{result.backend_run_id}.json"
                    self.assertEqual(state.exists(), policy != "remove")

    async def test_timeout_and_cancellation_terminate_fake_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeout_result = await self.fake_backend(root / "timeout-state").execute(
                provider(("fake-sleep",), timeout=0.1),
                "wait",
                None,
                root / "timeout-work",
            )
            self.assertEqual(timeout_result.status, "timeout")
            self.assertEqual(timeout_result.cleanup_status, "removed")

            cancel_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            loop.call_later(0.1, cancel_event.set)
            cancelled = await self.fake_backend(root / "cancel-state").execute(
                provider(("fake-sleep",), timeout=5.0),
                "wait",
                None,
                root / "cancel-work",
                cancel_event=cancel_event,
            )
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.cancel_reason, "project stop requested")
            self.assertEqual(cancelled.cleanup_status, "removed")

    async def test_missing_container_runtime_does_not_affect_local_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable = await ContainerExecutionBackend(
                ContainerBackendConfig(
                    runtime_command=("definitely-missing-qinyuan-runtime",),
                    default_image="fake:image",
                )
            ).execute(
                provider(("fake-echo",)),
                "request",
                None,
                root / "container",
            )
            local = await LocalExecutionBackend().execute(
                provider((sys.executable, "-c", "print('local-ok')"), image=None),
                "request",
                None,
                root / "local",
            )
            unavailable_stderr = Path(unavailable.stderr_path).read_text(
                encoding="utf-8"
            )

        self.assertEqual(unavailable.status, "unavailable")
        self.assertIn("container runtime", unavailable_stderr)
        self.assertEqual(local.status, "success")
        self.assertEqual(local.output, "local-ok")


if __name__ == "__main__":
    unittest.main()
