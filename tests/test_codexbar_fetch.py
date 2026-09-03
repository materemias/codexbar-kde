from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
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
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {
                "last_reset_at": "2026-08-31T02:34:27.000Z",
                "cadence": {"recent_median_days": "inf"},
            }
        ).encode()
        response.__enter__.return_value = response

        output = io.StringIO()
        with (
            mock.patch.object(fetch, "_forecast_cache_read", return_value=None),
            mock.patch.object(fetch.urllib.request, "urlopen", return_value=response),
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
                        fetch.urllib.request,
                        "urlopen",
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
                        fetch.urllib.request,
                        "urlopen",
                        side_effect=OSError("offline"),
                    ):
                        result = fetch._fetch_forecast("http://127.0.0.1:9", 1)

        self.assertTrue(result["ok"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["expectedAt"])


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
