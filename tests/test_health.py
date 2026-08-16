import unittest

from orchestrator.health import (
    ProbeResult,
    classify_probe_failure,
    classify_worker_failure,
    probe_provider,
    redact_error,
    summarize_failure,
)
from worker.models import HealthCheckConfig, ProviderConfig, WorkerResult


class HealthClassificationTests(unittest.TestCase):
    def test_authentication_failure_is_classified_without_secret_echo(self) -> None:
        detail = "Error: authentication failed (403) Authorization: Bearer sk-secret-value"
        result = WorkerResult(
            "codex",
            10,
            "failed",
            1,
            0.1,
            detail,
            "stdout",
            "missing-stderr",
            "work",
        )

        category = classify_worker_failure(result, detail)
        summary = summarize_failure(category, result.status, detail)

        self.assertEqual(category, "auth_failed")
        self.assertEqual(summary, "authentication failed (403); sign in before retrying")
        self.assertNotIn("sk-secret-value", summary)
        self.assertNotIn("sk-secret-value", redact_error(detail))

    def test_target_http_status_is_not_provider_authentication(self) -> None:
        result = WorkerResult(
            "q-subagent",
            10,
            "failed",
            1,
            0.1,
            "HTTP/1.1 403 Forbidden",
            "stdout",
            "stderr",
            "work",
        )

        self.assertEqual(
            classify_worker_failure(result, "HTTP/1.1 403 Forbidden"),
            "cooldown",
        )

    def test_explicit_provider_http_auth_diagnostic_is_classified(self) -> None:
        result = WorkerResult(
            "codex",
            10,
            "failed",
            1,
            0.1,
            "Error: 403 Forbidden",
            "stdout",
            "stderr",
            "work",
        )

        self.assertEqual(
            classify_worker_failure(result, "Error: 403 Forbidden"),
            "auth_failed",
        )

    def test_temporary_probe_failure_enters_cooldown(self) -> None:
        result = ProbeResult("failed", "503 Service Unavailable")

        self.assertEqual(classify_probe_failure(result), "cooldown")

    def test_missing_worker_command_is_classified_as_missing(self) -> None:
        result = WorkerResult(
            "codex",
            10,
            "unavailable",
            None,
            0.1,
            "",
            "stdout",
            "stderr",
            "work",
        )

        category = classify_worker_failure(
            result,
            "cannot start provider: [WinError 2] file not found",
        )

        self.assertEqual(category, "missing")


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_missing_without_starting_process(self) -> None:
        provider = ProviderConfig(
            "missing",
            True,
            10,
            ("unused",),
            5,
            health_check=HealthCheckConfig(
                ("definitely-not-a-real-provider-cli", "--version"),
                0.1,
                1,
            ),
        )

        result = await probe_provider(provider)

        self.assertEqual(result.status, "missing")


if __name__ == "__main__":
    unittest.main()
