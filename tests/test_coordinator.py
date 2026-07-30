import asyncio
import unittest

from orchestrator.coordinator import ExecutionCoordinator, PermitCancelled
from worker.models import CoordinatorConfig


class ExecutionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for_counts(
        self,
        coordinator: ExecutionCoordinator,
        *,
        running: int,
        pending: int,
    ) -> dict[str, object]:
        for _ in range(100):
            snapshot = await coordinator.snapshot()
            if (
                snapshot["running"]["global"] == running
                and snapshot["pending"]["total"] == pending
            ):
                return snapshot
            await asyncio.sleep(0)
        self.fail(
            f"coordinator counts did not reach running={running}, "
            f"pending={pending}: {await coordinator.snapshot()}"
        )

    @staticmethod
    async def hold_permit(
        coordinator: ExecutionCoordinator,
        release: asyncio.Event,
        *,
        project: str,
        provider: str,
        kind: str = "execution",
        priority: int = 0,
        provider_limit: int = 10,
    ) -> None:
        async with coordinator.permit(
            project_id=project,
            provider=provider,
            kind=kind,
            priority=priority,
            provider_limit=provider_limit,
        ):
            await release.wait()

    async def test_enforces_all_quotas_and_reserves_execution_capacity(self) -> None:
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=3,
                max_project_running=2,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=10,
            )
        )
        release = asyncio.Event()
        tasks = [
            asyncio.create_task(
                self.hold_permit(
                    coordinator,
                    release,
                    project="p1",
                    provider="a",
                    kind="planning",
                    provider_limit=1,
                )
            ),
            asyncio.create_task(
                self.hold_permit(
                    coordinator,
                    release,
                    project="p1",
                    provider="b",
                    kind="planning",
                )
            ),
            asyncio.create_task(
                self.hold_permit(
                    coordinator,
                    release,
                    project="p1",
                    provider="b",
                )
            ),
            asyncio.create_task(
                self.hold_permit(
                    coordinator,
                    release,
                    project="p2",
                    provider="a",
                    provider_limit=1,
                )
            ),
            asyncio.create_task(
                self.hold_permit(
                    coordinator,
                    release,
                    project="p2",
                    provider="c",
                )
            ),
        ]

        snapshot = await self.wait_for_counts(
            coordinator,
            running=3,
            pending=2,
        )
        self.assertEqual(snapshot["running"]["planning"], 1)
        self.assertEqual(snapshot["running"]["execution"], 2)
        self.assertEqual(snapshot["running"]["by_project"], {"p1": 2, "p2": 1})
        self.assertEqual(snapshot["running"]["by_provider"]["a"], 1)

        release.set()
        await asyncio.gather(*tasks)
        await self.wait_for_counts(coordinator, running=0, pending=0)

    async def test_round_robins_projects_under_pressure(self) -> None:
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=1,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=10,
            )
        )
        blocker_release = asyncio.Event()
        blocker = asyncio.create_task(
            self.hold_permit(
                coordinator,
                blocker_release,
                project="blocker",
                provider="blocker",
            )
        )
        await self.wait_for_counts(coordinator, running=1, pending=0)
        order: list[str] = []

        async def record(project: str, provider: str) -> None:
            async with coordinator.permit(
                project_id=project,
                provider=provider,
                kind="execution",
                priority=0,
                provider_limit=10,
            ):
                order.append(project)
                await asyncio.sleep(0)

        tasks = [
            asyncio.create_task(record("p1", "p1-a")),
            asyncio.create_task(record("p1", "p1-b")),
            asyncio.create_task(record("p2", "p2-a")),
            asyncio.create_task(record("p3", "p3-a")),
        ]
        await self.wait_for_counts(coordinator, running=1, pending=4)
        blocker_release.set()
        await asyncio.gather(blocker, *tasks)

        self.assertEqual(order, ["p1", "p2", "p3", "p1"])

    async def test_priority_orders_requests_within_project(self) -> None:
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=1,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=10,
            )
        )
        release = asyncio.Event()
        blocker = asyncio.create_task(
            self.hold_permit(
                coordinator,
                release,
                project="blocker",
                provider="blocker",
            )
        )
        await self.wait_for_counts(coordinator, running=1, pending=0)
        order: list[str] = []

        async def record(provider: str, priority: int) -> None:
            async with coordinator.permit(
                project_id="project",
                provider=provider,
                kind="execution",
                priority=priority,
                provider_limit=1,
            ):
                order.append(provider)

        low = asyncio.create_task(record("low", 1))
        high = asyncio.create_task(record("high", 100))
        await self.wait_for_counts(coordinator, running=1, pending=2)
        release.set()
        await asyncio.gather(blocker, low, high)

        self.assertEqual(order, ["high", "low"])

    async def test_aging_prevents_low_priority_starvation(self) -> None:
        now = [0.0]
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=1,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=1,
            ),
            clock=lambda: now[0],
        )
        release = asyncio.Event()
        blocker = asyncio.create_task(
            self.hold_permit(
                coordinator,
                release,
                project="blocker",
                provider="blocker",
            )
        )
        await self.wait_for_counts(coordinator, running=1, pending=0)
        order: list[str] = []

        async def record(provider: str, priority: int) -> None:
            async with coordinator.permit(
                project_id="project",
                provider=provider,
                kind="execution",
                priority=priority,
                provider_limit=1,
            ):
                order.append(provider)

        low = asyncio.create_task(record("low", 0))
        await self.wait_for_counts(coordinator, running=1, pending=1)
        now[0] = 5.0
        high = asyncio.create_task(record("high", 3))
        await self.wait_for_counts(coordinator, running=1, pending=2)
        release.set()
        await asyncio.gather(blocker, low, high)

        self.assertEqual(order, ["low", "high"])

    async def test_cancelled_waiter_is_removed_and_capacity_is_reusable(self) -> None:
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=1,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=1,
            )
        )
        release = asyncio.Event()
        blocker = asyncio.create_task(
            self.hold_permit(
                coordinator,
                release,
                project="blocker",
                provider="blocker",
            )
        )
        await self.wait_for_counts(coordinator, running=1, pending=0)
        cancel_event = asyncio.Event()

        async def wait_then_cancel() -> None:
            with self.assertRaises(PermitCancelled):
                async with coordinator.permit(
                    project_id="project",
                    provider="provider",
                    kind="execution",
                    priority=0,
                    provider_limit=1,
                    cancel_event=cancel_event,
                ):
                    self.fail("cancelled waiter received a permit")

        waiter = asyncio.create_task(wait_then_cancel())
        await self.wait_for_counts(coordinator, running=1, pending=1)
        cancel_event.set()
        await waiter
        await self.wait_for_counts(coordinator, running=1, pending=0)
        release.set()
        await blocker

        reusable = asyncio.Event()
        reusable.set()
        await self.hold_permit(
            coordinator,
            reusable,
            project="project",
            provider="provider",
        )
        await self.wait_for_counts(coordinator, running=0, pending=0)


if __name__ == "__main__":
    unittest.main()
