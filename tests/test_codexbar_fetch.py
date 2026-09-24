from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "codexbar_fetch.py"
)
SPEC = importlib.util.spec_from_file_location("codexbar_fetch", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


@contextlib.contextmanager
def forecast_server(body: bytes, interval: float = 0):
    stopped = threading.Event()
    requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.set()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if interval:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if stopped.wait(interval):
                            break
                else:
                    self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/forecast", requested
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join()


class ProviderFailureTests(unittest.TestCase):
    def test_invalid_executable_emits_normalized_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "invalid-cli"
            cli.write_text("not an executable format\n", encoding="utf-8")
            cli.chmod(0o700)
            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT_PATH),
                    "--cli-path", str(cli), "--providers", "kilo",
                ],
                capture_output=True, text=True, timeout=3, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["fatal"])
        self.assertIsNone(payload["cliVersion"])
        self.assertEqual(payload["providers"][0]["id"], "kilo")
        self.assertFalse(payload["providers"][0]["ok"])
        self.assertEqual(payload["providers"][0]["error"]["code"], "cli_error")

    def test_process_oserrors_become_provider_errors(self) -> None:
        for error in (PermissionError("denied"), OSError("I/O error")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(fetch.subprocess, "run", side_effect=error):
                    result = fetch._run_cli("/bin/true", "kilo", None, 1)[0]
                self.assertEqual(result["id"], "kilo")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "cli_error")

    def test_malformed_provider_keeps_healthy_results(self) -> None:
        def cli_result(command, **kwargs):
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "CodexBar 0.56.3", "")
            provider = command[command.index("--provider") + 1]
            primary = {"usedPercent": 12}
            if provider == "kilo":
                primary["resetDescription"] = 42
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"usage": {"primary": primary}}), ""
            )

        output = io.StringIO()
        with (
            mock.patch.object(fetch.subprocess, "run", side_effect=cli_result),
            mock.patch("sys.stdout", output),
        ):
            exit_code = fetch.main(
                ["--cli-path", "/bin/true", "--providers", "kilo,codex"]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNone(payload["fatal"])
        failed, healthy = payload["providers"]
        self.assertEqual(failed["id"], "kilo")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "provider")
        self.assertEqual(healthy["id"], "codex")
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["primary"]["usedPercent"], 12)

    def test_timeout_requires_a_finite_positive_number(self) -> None:
        for value in ("0", "-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value):
                with (
                    mock.patch("sys.stderr", io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    fetch.main(
                        ["--cli-path", "/bin/true", f"--timeout={value}"]
                    )
                self.assertEqual(raised.exception.code, 2)


class ResetCreditTests(unittest.TestCase):
    def test_counts_available_credits_and_soonest_expiry(self) -> None:
        record = fetch._normalize_record("codex", {"usage": {
            "primary": {"usedPercent": 1},
            "codexResetCredits": {"availableCount": 3, "credits": [
                {"status": "available", "expires_at": "2026-10-04T22:10:12Z"},
                {"status": "available", "expires_at": "2026-10-04T00:47:58Z"},
                {"status": "used", "expires_at": "2026-09-01T00:00:00Z"},
            ]},
        }})
        self.assertEqual(record["resetCredits"], {
            "count": 2,
            "soonestExpiresAt": "2026-10-04T00:47:58+00:00",
        })

    def test_missing_credits_yield_none(self) -> None:
        record = fetch._normalize_record("codex", {"usage": {"primary": {"usedPercent": 1}}})
        self.assertIsNone(record["resetCredits"])

    def test_claude_counts_only_active_unpaused_grants(self) -> None:
        now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
        credits = fetch._claude_reset_credits({"eligible": True, "grants": [
            {"resets_left": 2, "starts_at": "2026-09-22T16:00:00+00:00",
             "ends_at": "2026-10-22T16:00:00+00:00"},
            {"resets_left": 1, "ends_at": "2026-10-01T00:00:00+00:00"},
            {"resets_left": 1, "paused": True, "ends_at": "2026-09-24T00:00:00+00:00"},
            {"resets_left": 1, "starts_at": "2026-09-30T00:00:00+00:00"},
            {"resets_left": 1, "ends_at": "2026-09-22T00:00:00+00:00"},
            {"resets_left": 0, "ends_at": "2026-09-25T00:00:00+00:00"},
        ]}, now)
        self.assertEqual(credits, {
            "count": 3,
            "soonestExpiresAt": "2026-10-01T00:00:00+00:00",
        })

    def test_claude_ineligible_status_yields_none(self) -> None:
        now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
        self.assertIsNone(fetch._claude_reset_credits(
            {"eligible": False, "ineligible_reason": "surface", "grants": []}, now
        ))


class ForecastTests(unittest.TestCase):
    def test_eta_uses_cadence_and_future_window_start(self) -> None:
        last_reset = dt.datetime(2026, 8, 31, 2, 34, 27, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 9, 2, 10, 0, tzinfo=dt.timezone.utc)

        eta = fetch._forecast_eta(last_reset, 2.1, 23, 2, now)

        self.assertEqual(eta, dt.datetime(2026, 9, 2, 23, tzinfo=dt.timezone.utc))

    def test_eta_keeps_projection_inside_window(self) -> None:
        last_reset = dt.datetime(2026, 9, 1, 22, 30, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 9, 2, 10, 0, tzinfo=dt.timezone.utc)

        eta = fetch._forecast_eta(last_reset, 25 / 24, 23, 2, now)

        self.assertEqual(eta, dt.datetime(2026, 9, 2, 23, 30, tzinfo=dt.timezone.utc))

    def test_eta_rolls_elapsed_projection(self) -> None:
        last_reset = dt.datetime(2026, 9, 1, 22, 30, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 9, 2, 23, 45, tzinfo=dt.timezone.utc)

        eta = fetch._forecast_eta(last_reset, 25 / 24, 23, 2, now)

        self.assertEqual(eta, dt.datetime(2026, 9, 3, 23, 30, tzinfo=dt.timezone.utc))

    def test_normalizes_forecast_fields(self) -> None:
        payload = fetch._normalize_forecast(
            {
                "updated_at": "2026-09-02T10:03:10.570Z",
                "last_reset_at": "2026-08-31T02:34:27.000Z",
                "probabilities": {
                    "raw_24h": 0.25,
                    "raw_48h": 0.45,
                },
                "time_window": {
                    "start_hour": 23,
                    "end_hour": 2,
                    "label": "11 PM - 2 AM",
                    "timezone": "UTC",
                },
                "cadence": {"recent_median_days": 2.1},
                "confidence": "low",
            }
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["prob24h"], 25.0)
        self.assertEqual(payload["prob48h"], 45.0)
        self.assertEqual(payload["windowStartHour"], 23)
        self.assertEqual(payload["windowEndHour"], 2)
        self.assertTrue(payload["expectedAt"])
        self.assertIsNone(payload["alertSummary"])

    def _forecast_with_alert(self, alert) -> dict:
        return fetch._normalize_forecast(
            {
                "last_reset_at": "2026-08-31T02:34:27.000Z",
                "cadence": {"recent_median_days": 2.1},
                "latest_alert": alert,
            }
        )

    def test_alert_newer_than_last_reset_surfaces_summary(self) -> None:
        payload = self._forecast_with_alert(
            {
                "source_at": "2026-09-01T18:00:00.000Z",
                "summary": "  Reset promised for 6pm PT.  ",
            }
        )

        self.assertEqual(payload["alertSummary"], "Reset promised for 6pm PT.")

    def test_alert_matching_last_reset_is_dropped(self) -> None:
        payload = self._forecast_with_alert(
            {
                "source_at": "2026-08-31T02:34:27.000Z",
                "summary": "Usage reset for every paid subscription.",
            }
        )

        self.assertIsNone(payload["alertSummary"])

    def test_alert_older_than_last_reset_is_dropped(self) -> None:
        payload = self._forecast_with_alert(
            {
                "source_at": "2026-08-29T12:00:00.000Z",
                "summary": "An earlier reset announcement.",
            }
        )

        self.assertIsNone(payload["alertSummary"])

    def test_missing_alert_yields_no_summary(self) -> None:
        self.assertIsNone(self._forecast_with_alert(None)["alertSummary"])

    def test_nonfinite_cadence_keeps_snapshot_healthy(self) -> None:
        body = json.dumps(
            {
                "last_reset_at": "2026-08-31T02:34:27.000Z",
                "cadence": {"recent_median_days": "inf"},
            }
        ).encode()

        output = io.StringIO()
        with (
            mock.patch.object(fetch, "_forecast_cache_read", return_value=None),
            mock.patch.object(fetch, "_read_http_body", return_value=body),
            mock.patch("sys.stdout", output),
        ):
            exit_code = fetch.main(
                [
                    "--cli-path",
                    "/bin/true",
                    "--providers",
                    "",
                    "--forecast-url",
                    "https://example.invalid/forecast",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNone(payload["fatal"])
        self.assertEqual(payload["providers"], [])
        self.assertFalse(payload["forecast"]["ok"])

    def test_failed_refresh_uses_stale_cache(self) -> None:
        cached = {
            "ok": True,
            "stale": False,
            "source": "codex-reset.com",
            "expectedAt": "2026-09-02T23:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "forecast.json"
            with mock.patch.object(fetch, "FORECAST_CACHE_PATH", str(cache_path)):
                fetch._forecast_cache_write(cached)
                with mock.patch.object(fetch, "FORECAST_CACHE_TTL", -1):
                    with mock.patch.object(
                        fetch,
                        "_read_http_body",
                        side_effect=OSError("offline"),
                    ):
                        result = fetch._fetch_forecast("http://127.0.0.1:9", 1)

        self.assertTrue(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["expectedAt"], cached["expectedAt"])

    def test_stale_cache_preserves_missing_eta(self) -> None:
        cached = {
            "ok": True,
            "stale": False,
            "source": "codex-reset.com",
            "expectedAt": None,
            "windowStartHour": 23,
            "windowEndHour": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "forecast.json"
            with mock.patch.object(fetch, "FORECAST_CACHE_PATH", str(cache_path)):
                fetch._forecast_cache_write(cached)
                with mock.patch.object(fetch, "FORECAST_CACHE_TTL", -1):
                    with mock.patch.object(
                        fetch,
                        "_read_http_body",
                        side_effect=OSError("offline"),
                    ):
                        result = fetch._fetch_forecast("http://127.0.0.1:9", 1)

        self.assertTrue(result["ok"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["expectedAt"])

    def test_successful_response_is_cached(self) -> None:
        body = json.dumps({
            "last_reset_at": "2026-09-14T02:00:00Z",
            "cadence": {"recent_median_days": 2},
        }).encode()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "forecast.json"
            with mock.patch.object(fetch, "FORECAST_CACHE_PATH", str(cache)):
                with forecast_server(body) as (url, _):
                    result = fetch._fetch_forecast(url, 2)
                cached = fetch._fetch_forecast(url, 0.1)
        self.assertTrue(result["ok"])
        self.assertFalse(result["stale"])
        self.assertEqual(cached, result)

    def test_slow_drip_has_total_deadline_and_retains_stale_cache(self) -> None:
        body = json.dumps({
            "last_reset_at": "2026-09-14T02:00:00Z",
            "cadence": {"recent_median_days": 2},
        }).encode()
        cached = {
            "ok": True,
            "stale": False,
            "source": "codex-reset.com",
            "expectedAt": "2026-09-16T02:00:00+00:00",
        }
        timeout = 0.4
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "forecast.json"
            with (
                mock.patch.object(fetch, "FORECAST_CACHE_PATH", str(cache)),
                mock.patch.object(fetch, "FORECAST_CACHE_TTL", -1),
                forecast_server(body, interval=0.05) as (url, requested),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                fetch._forecast_cache_write(cached)
                started = time.monotonic()
                result = pool.submit(fetch._fetch_forecast, url, timeout).result(
                    timeout=3
                )
                elapsed = time.monotonic() - started
        self.assertLess(elapsed, timeout + 0.75)
        self.assertTrue(requested.is_set())
        self.assertEqual(result, dict(cached, stale=True))

    def test_oversized_valid_forecast_is_rejected(self) -> None:
        body = json.dumps({
            "last_reset_at": "2026-09-14T02:00:00Z",
            "cadence": {"recent_median_days": 2},
        }).encode() + b" " * (fetch.FORECAST_MAX_BODY_BYTES + 1)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "forecast.json"
            with (
                mock.patch.object(fetch, "FORECAST_CACHE_PATH", str(cache)),
                forecast_server(body) as (url, _),
            ):
                result = fetch._fetch_forecast(url, 2)
                self.assertFalse(cache.exists())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forecast_unavailable")


class CliVersionTests(unittest.TestCase):
    def _run(self, **kwargs) -> str | None:
        completed = mock.MagicMock()
        completed.returncode = kwargs.get("returncode", 0)
        completed.stdout = kwargs.get("stdout", "")
        with mock.patch.object(
            fetch.subprocess,
            "run",
            side_effect=kwargs.get("side_effect"),
            return_value=completed,
        ):
            return fetch._cli_version("/bin/codexbar")

    def test_parses_version_from_cli_banner(self) -> None:
        self.assertEqual(self._run(stdout="CodexBar 0.56.3\n"), "0.56.3")

    def test_nonzero_exit_yields_none(self) -> None:
        self.assertIsNone(self._run(returncode=64, stdout="Unknown flag '--version'"))

    def test_unparseable_output_yields_none(self) -> None:
        self.assertIsNone(self._run(stdout="CodexBar\n"))

    def test_missing_binary_yields_none(self) -> None:
        self.assertIsNone(self._run(side_effect=FileNotFoundError("no cli")))

    def test_snapshot_carries_cli_version(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(fetch, "_cli_version", return_value="0.56.3"),
            mock.patch("sys.stdout", output),
        ):
            exit_code = fetch.main(["--cli-path", "/bin/true", "--providers", ""])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["cliVersion"], "0.56.3")


class OpenRouterBalanceTests(unittest.TestCase):
    CREDITS_DETAILS = [
        {
            "title": "Credits",
            "rows": [
                {"label": "Remaining", "value": "$6.17"},
                {"label": "Used", "value": "$126.83"},
            ],
        }
    ]

    def _normalize(self, usage: dict) -> dict:
        return fetch._normalize_record(
            "openrouter", {"provider": "openrouter", "source": "api", "usage": usage}
        )

    def test_details_row_supplies_balance_without_openrouter_usage(self) -> None:
        # CLI 0.56.3 shape: no openRouterUsage at all.
        record = self._normalize(
            {
                "primary": {"usedPercent": 100.0, "resetDescription": "placeholder"},
                "loginMethod": "Balance: $6.17",
                "details": self.CREDITS_DETAILS,
            }
        )

        self.assertEqual(record["balanceText"], "$6.17 left")
        self.assertIsNone(record["primary"])

    def test_login_method_supplies_balance_without_details(self) -> None:
        record = self._normalize(
            {"primary": {"usedPercent": 100.0}, "loginMethod": "Balance: $6.17"}
        )

        self.assertEqual(record["balanceText"], "$6.17 left")
        self.assertIsNone(record["primary"])

    def test_zero_key_limit_still_shows_balance(self) -> None:
        record = self._normalize(
            {
                "primary": {"usedPercent": 100.0},
                "openRouterUsage": {"keyLimit": 0},
                "details": self.CREDITS_DETAILS,
            }
        )

        self.assertEqual(record["balanceText"], "$6.17 left")
        self.assertIsNone(record["primary"])

    def test_openrouter_usage_without_key_limit_still_shows_balance(self) -> None:
        record = self._normalize(
            {
                "primary": {"usedPercent": 100.0},
                "openRouterUsage": {"balance": 6.17},
                "details": self.CREDITS_DETAILS,
            }
        )

        self.assertEqual(record["balanceText"], "$6.17 left")
        self.assertIsNone(record["primary"])

    def test_key_limit_renders_bar_and_no_balance_text(self) -> None:
        record = self._normalize(
            {
                "primary": {"usedPercent": 100.0},
                "openRouterUsage": {"keyLimit": 20, "keyUsageMonthly": 5.0},
                "details": self.CREDITS_DETAILS,
            }
        )

        self.assertIsNone(record["balanceText"])
        self.assertEqual(record["primary"]["usedPercent"], 25.0)
        self.assertEqual(record["primary"]["resetDescription"], "$5.00 / $20")

    def test_thousands_separator_parses(self) -> None:
        record = self._normalize(
            {
                "details": [
                    {
                        "title": "Credits",
                        "rows": [{"label": "Remaining", "value": "$1,234.50"}],
                    }
                ]
            }
        )

        self.assertEqual(record["balanceText"], "$1234.50 left")

    def test_unparseable_balance_yields_none(self) -> None:
        record = self._normalize(
            {
                "primary": {"usedPercent": 100.0},
                "loginMethod": "Balance: unavailable",
                "details": [
                    {
                        "title": "Credits",
                        "rows": [{"label": "Remaining", "value": "Unavailable"}],
                    }
                ],
            }
        )

        self.assertIsNone(record["balanceText"])
        self.assertIsNone(record["primary"])

    def test_missing_usage_yields_none(self) -> None:
        record = self._normalize({})

        self.assertIsNone(record["balanceText"])
        self.assertIsNone(record["primary"])


if __name__ == "__main__":
    unittest.main()
