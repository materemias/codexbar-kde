#!/usr/bin/env python3
"""CodexBar KDE plasmoid data fetcher.

Runs `codexbar usage --json --provider <id> [--source <s>]` for each enabled
provider in parallel, merges the per-provider results into a single JSON
document on stdout. The QML widget calls this once per polling tick.

Usage:
  codexbar_fetch.py --cli-path PATH --providers codex,claude,openrouter,kilo

With --forecast-url it also attaches a `forecast` object describing when the next
OpenAI usage-limit reset is expected (data from codex-reset.com).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Per-provider source flag. None = let the CLI auto-pick.
# Claude defaults to OAuth so we get extraRateWindows (Claude Design,
# Daily Routines, …) — CLI source only returns primary/secondary/tertiary.
PROVIDER_SOURCE: dict[str, str | None] = {
    "codex": "oauth",
    "claude": "oauth",
    "zai": None,
    "openrouter": None,
    "kilo": None,
}

# Fallback sources tried in order when the primary source errors (e.g. 429).
PROVIDER_FALLBACK_SOURCES: dict[str, list[str]] = {
    "codex": ["cli"],
    "claude": ["cli"],
}


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _result_error(provider: str, code: str, message: str, source: str | None = None) -> dict:
    return {
        "id": provider,
        "ok": False,
        "source": source,
        "error": {"code": code, "message": message},
    }


def _normalize_record(provider: str, record: dict) -> dict:
    raw_usage = record.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw_identity = usage.get("identity")
    identity = raw_identity if isinstance(raw_identity, dict) else {}
    raw_email = usage.get("accountEmail") or record.get("account")
    account_email = raw_email.strip() if isinstance(raw_email, str) else None
    raw_login_method = usage.get("loginMethod") or identity.get("loginMethod")
    login_method = raw_login_method.strip() if isinstance(raw_login_method, str) else None

    if record.get("error"):
        raw_error = record["error"]
        error = raw_error if isinstance(raw_error, dict) else {}
        result = _result_error(
            provider,
            str(error.get("kind", "provider")),
            str(error.get("message") or (
                raw_error if isinstance(raw_error, str) else "unknown error"
            )),
            record.get("source"),
        )
        result.update({
            "identity": identity,
            "loginMethod": login_method,
            "accountEmail": account_email,
        })
        return result

    primary = usage.get("primary")
    or_usage = usage.get("openRouterUsage")
    balance_text: str | None = None
    if provider == "openrouter" and or_usage and isinstance(or_usage, dict):
        # The CodexBar CLI returns primary.usedPercent=100 as a placeholder.
        # Replace it with the per-key allowance when one exists.
        key_limit = or_usage.get("keyLimit")
        if isinstance(key_limit, (int, float)) and key_limit > 0:
            monthly = or_usage.get("keyUsageMonthly")
            monthly = monthly if isinstance(monthly, (int, float)) else 0.0
            primary = {
                "usedPercent": min(100.0, (monthly / key_limit) * 100.0),
                "resetDescription": f"${monthly:.2f} / ${key_limit:.0f}",
            }
        else:
            primary = None
    elif provider == "kilo" and isinstance(primary, dict):
        # Kilo reports used/total credits. With auto-topup off this is a
        # balance, not a recurring window, so show the amount in the header.
        m = re.match(
            r"^\s*([\d.]+)\s*/\s*([\d.]+)\s*credits?",
            primary.get("resetDescription") or "",
        )
        if m:
            try:
                used = float(m.group(1))
                total = float(m.group(2))
            except ValueError:
                used = total = None
            if used is not None and total is not None:
                balance_text = f"${max(0.0, total - used):.2f} left"
                primary = None
    raw_extras = usage.get("extraRateWindows")
    extra_rate_windows = [
        extra
        for extra in (raw_extras if isinstance(raw_extras, list) else [])
        if isinstance(extra, dict)
        and not (
            provider == "codex"
            and str(extra.get("id", "")).startswith("codex-spark")
        )
    ]
    return {
        "id": provider,
        "ok": True,
        "source": record.get("source"),
        "identity": identity,
        "loginMethod": login_method,
        "accountEmail": account_email,
        "primary": primary,
        "secondary": usage.get("secondary"),
        "tertiary": usage.get("tertiary"),
        "extraRateWindows": extra_rate_windows,
        "openRouterUsage": or_usage,
        "balanceText": balance_text,
        "updatedAt": usage.get("updatedAt"),
        "error": None,
    }


def _run_cli(cli: str, provider: str, source: str | None, timeout: float) -> list[dict]:
    """Invoke `codexbar usage` and normalize every returned account."""
    cmd = [cli, "usage", "--json", "--provider", provider]
    if provider == "codex":
        cmd.append("--all-accounts")
    if source:
        cmd += ["--source", source]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return [_result_error(provider, "cli_missing", f"CLI not found at {cli}")]
    except subprocess.TimeoutExpired:
        return [_result_error(provider, "timeout", f"CLI timed out after {timeout}s")]

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return [
            _result_error(
                provider,
                "no_output",
                (proc.stderr or "empty stdout").strip()[:400],
            )
        ]
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [_result_error(provider, "parse", f"{exc}: {stdout[:200]}")]

    records = payload if isinstance(payload, list) else [payload]
    if not records or any(not isinstance(record, dict) for record in records):
        return [_result_error(provider, "shape", "unexpected CLI payload")]
    results = [_normalize_record(provider, record) for record in records]
    account_count = len(results)
    for result in results:
        result["accountCount"] = account_count
    return results


def _fetch_provider(cli: str, provider: str, timeout: float) -> list[dict]:
    primary_source = PROVIDER_SOURCE.get(provider)
    results = _run_cli(cli, provider, primary_source, timeout)
    if any(result.get("ok") for result in results):
        return results
    for fallback in PROVIDER_FALLBACK_SOURCES.get(provider, []):
        retries = _run_cli(cli, provider, fallback, timeout)
        if any(retry.get("ok") for retry in retries):
            return retries
    return results


def _cli_version(cli: str, timeout: float = 5.0) -> str | None:
    """Return the codexbar CLI version, or None when it cannot be determined.

    `codexbar --version` prints e.g. "CodexBar 0.56.3". Any failure (missing
    binary, non-zero exit, old CLI without the flag, unexpected text) is
    silent: the widget simply hides the version label.
    """
    try:
        proc = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?", proc.stdout or "")
    return match.group(0) if match else None

def _highest(records: list[dict]) -> tuple[str | None, float]:
    best_id: str | None = None
    best_pct = -1.0
    for rec in records:
        if not rec.get("ok"):
            continue
        windows: list[dict] = []
        for slot in ("primary", "secondary", "tertiary"):
            w = rec.get(slot)
            if isinstance(w, dict):
                windows.append(w)
        for extra in rec.get("extraRateWindows") or []:
            if isinstance(extra, dict):
                w = extra.get("window")
                if isinstance(w, dict):
                    windows.append(w)
        for w in windows:
            try:
                pct = float(w.get("usedPercent") or 0.0)
            except (TypeError, ValueError):
                continue
            if pct > best_pct:
                best_pct = pct
                best_id = rec.get("id")
    return best_id, max(best_pct, 0.0)


# codex-reset.com publishes a probabilistic forecast for the next OpenAI
# usage-limit reset. It exposes no point estimate, only per-horizon
# probabilities plus the observed cadence, so the ETA below is derived:
# last confirmed reset + recent median cadence, snapped into the hour window
# resets historically land in.
FORECAST_TIMEOUT = 8.0
# Resets happen every few days and the model refreshes slowly, so a short
# cache keeps a 30-second poll tick from hammering a third-party endpoint.
FORECAST_CACHE_TTL = 900.0
FORECAST_CACHE_PATH = "~/.codexbar/forecast_cache.json"
FORECAST_MAX_MEDIAN_DAYS = 365.0


def _as_float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_hour(value) -> int | None:
    parsed = _as_float(value)
    if parsed is None or not parsed.is_integer() or not 0 <= parsed <= 23:
        return None
    return int(parsed)


def _parse_iso(value) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _forecast_eta(
    last_reset: _dt.datetime | None,
    median_days: float | None,
    start_hour: int | None,
    end_hour: int | None,
    now: _dt.datetime,
) -> _dt.datetime | None:
    """Project the next reset from the last one plus the observed cadence."""
    if last_reset is None or median_days is None or median_days <= 0:
        return None
    projected = last_reset + _dt.timedelta(days=median_days)
    eta = projected
    if start_hour is not None and end_hour is not None:
        start_at = projected.replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        end_at = start_at.replace(
            hour=end_hour, minute=0, second=0, microsecond=0
        )
        if end_hour <= start_hour:
            end_at += _dt.timedelta(days=1)
        if projected < start_at:
            eta = start_at
        elif projected >= end_at:
            eta = start_at + _dt.timedelta(days=1)
    # Keep the displayed instant in the future by rolling it forward one day
    # at a time when the projection has already elapsed.
    guard = 0
    while eta <= now and guard < 400:
        eta += _dt.timedelta(days=1)
        guard += 1
    return eta


def _forecast_cache_read(max_age: float | None) -> dict | None:
    try:
        with open(_expand(FORECAST_CACHE_PATH), "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    payload = cached.get("forecast")
    if not isinstance(payload, dict):
        return None
    cached_at = _as_float(cached.get("cachedAt")) or 0.0
    if max_age is not None and (time.time() - cached_at) > max_age:
        return None
    return payload


def _forecast_cache_write(payload: dict) -> None:
    path = _expand(FORECAST_CACHE_PATH)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cachedAt": time.time(), "forecast": payload}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def _normalize_forecast(data: dict) -> dict:
    def _obj(key: str) -> dict:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    probs = _obj("probabilities")
    window = _obj("time_window")
    cadence = _obj("cadence")

    def _percent(rounded_key: str, raw_key: str) -> float | None:
        rounded = _as_float(probs.get(rounded_key))
        if rounded is not None:
            return rounded
        raw = _as_float(probs.get(raw_key))
        return raw * 100.0 if raw is not None else None

    now = _dt.datetime.now(_dt.timezone.utc)
    last_reset = _parse_iso(data.get("last_reset_at"))
    median_days = _as_float(cadence.get("recent_median_days"))
    if (
        last_reset is None
        or median_days is None
        or median_days <= 0
        or median_days > FORECAST_MAX_MEDIAN_DAYS
    ):
        raise ValueError("forecast missing a valid reset cadence")
    window_start_hour = _as_hour(window.get("start_hour"))
    window_end_hour = _as_hour(window.get("end_hour"))
    eta = _forecast_eta(
        last_reset, median_days, window_start_hour, window_end_hour, now
    )
    # The latest alert only explains the *next* reset when it postdates the last
    # one; otherwise it is just the announcement of the reset already recorded.
    alert = data.get("latest_alert")
    alert_summary = None
    if isinstance(alert, dict):
        alert_at = _parse_iso(alert.get("source_at"))
        summary = alert.get("summary")
        if (
            alert_at is not None
            and alert_at > last_reset
            and isinstance(summary, str)
            and summary.strip()
        ):
            alert_summary = summary.strip()
    teased = data.get("teased_window")
    return {
        "ok": True,
        "stale": False,
        "source": "codex-reset.com",
        "fetchedAt": now.isoformat(),
        "modelUpdatedAt": (_parse_iso(data.get("updated_at")) or now).isoformat(),
        "expectedAt": eta.isoformat() if eta is not None else None,
        "windowLabel": str(window.get("label") or ""),
        "windowTimezone": str(window.get("timezone") or ""),
        "windowStartHour": window_start_hour,
        "windowEndHour": window_end_hour,
        "teasedWindow": teased if isinstance(teased, str) and teased.strip() else None,
        "prob24h": _percent("rounded_24h", "raw_24h"),
        "prob48h": _percent("rounded_48h", "raw_48h"),
        "confidence": str(data.get("confidence") or ""),
        "lastResetAt": last_reset.isoformat() if last_reset is not None else None,
        "alertSummary": alert_summary,
        "medianDays": median_days,
        "error": None,
    }


def _fetch_forecast(url: str, timeout: float) -> dict:
    """Fetch and normalize the reset forecast without raising."""
    fresh = _forecast_cache_read(FORECAST_CACHE_TTL)
    if fresh is not None:
        return fresh
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "codexbar-kde", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace").strip()
        if not body:
            raise ValueError("empty response body")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        payload = _normalize_forecast(data)
    except Exception as exc:  # noqa: BLE001 - the endpoint cannot break usage
        stale = _forecast_cache_read(None)
        if stale is not None and stale.get("ok") is True:
            return dict(stale, stale=True)
        return {
            "ok": False,
            "stale": False,
            "source": "codex-reset.com",
            "expectedAt": None,
            "error": {"code": "forecast_unavailable", "message": str(exc)},
        }
    _forecast_cache_write(payload)
    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli-path", required=True)
    parser.add_argument(
        "--providers",
        default="codex,claude,openrouter,kilo",
        help="Comma-separated provider ids to query.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--forecast-url",
        default=None,
        help="Fetch the Codex reset forecast from this URL.",
    )
    args = parser.parse_args(argv)

    cli = _expand(args.cli_path)
    if not (os.path.isfile(cli) and os.access(cli, os.X_OK)) and not shutil.which(cli):
        out = {
            "updatedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "fatal": {
                "code": "cli_missing",
                "message": f"codexbar CLI not found or not executable: {cli}",
            },
            "providers": [],
            "highestProvider": None,
            "highestPercent": 0,
            "forecast": None,
            "cliVersion": None,
        }
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    results: list[dict] = []
    forecast_future = None
    with ThreadPoolExecutor(
        max_workers=max(1, len(providers) + int(bool(args.forecast_url)))
    ) as pool:
        futures = {
            pool.submit(_fetch_provider, cli, p, args.timeout): p for p in providers
        }
        if args.forecast_url:
            forecast_future = pool.submit(
                _fetch_forecast, args.forecast_url, FORECAST_TIMEOUT
            )
        for fut in as_completed(futures):
            results.extend(fut.result())

    # Preserve the requested provider order in output.
    order = {p: i for i, p in enumerate(providers)}
    results.sort(key=lambda r: order.get(r["id"], 999))

    best_id, best_pct = _highest(results)
    out = {
        "updatedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "highestProvider": best_id,
        "highestPercent": best_pct,
        "providers": results,
        "fatal": None,
        "forecast": forecast_future.result() if forecast_future is not None else None,
        "cliVersion": _cli_version(cli),
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
