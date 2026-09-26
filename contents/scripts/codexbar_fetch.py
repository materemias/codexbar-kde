#!/usr/bin/env python3
"""CodexBar KDE plasmoid data fetcher.

Runs `codexbar usage --json --provider <id> [--source <s>]` for each enabled
provider in parallel, merges the per-provider results into a single JSON
document on stdout. The QML widget calls this once per polling tick.

Usage:
  codexbar_fetch.py --cli-path PATH --providers codex,claude,zai,opencodego,openrouter,kilo,typesafe

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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Per-provider source flag. None = let the CLI auto-pick.
# Claude defaults to OAuth so we get extraRateWindows (Claude Design,
# Daily Routines, …) — CLI source only returns primary/secondary/tertiary.
PROVIDER_SOURCE: dict[str, str | None] = {
    "codex": "oauth",
    "claude": "oauth",
    "zai": None,
    # api = real opencode.ai quota (needs apiKey in ~/.codexbar/config.json);
    # the CLI's auto pick is the "local" estimate, which is far off.
    "opencodego": "api",
    "openrouter": None,
    "kilo": None,
    # Balance-only. Linux has no browser cookie import, so the CLI needs
    # cookieSource=manual + cookieHeader in ~/.codexbar/config.json.
    "typesafe": None,
}

# Fallback sources tried in order when the primary source errors (e.g. 429).
PROVIDER_FALLBACK_SOURCES: dict[str, list[str]] = {
    "codex": ["cli"],
    "claude": ["cli"],
    "opencodego": ["local"],
}

CODEX_ROTATION_DESCRIPTIONS = {
    "FILL": "Pros share traffic, stop at 85%.",
    "TARGET": "One Pro drains to 100%, redeems.",
    "EXPIRY-BURN": "Burn accounts whose resets expire soon.",
    "BURN": "Global reset announced; Pros stop 95%.",
    "NO-BANK": "No banked resets; Pros stop 95%.",
    "IDLE": "No Pro accounts logged in.",
}


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _codex_rotation_accounts(state: dict) -> dict:
    """Match unambiguous account labels to omp's owned-key block list."""
    owned = state.get("owned")
    accounts = state.get("accounts")
    if (
        not isinstance(owned, dict)
        or any(not re.fullmatch(r"0|[1-9][0-9]*", key) for key in owned)
        or not isinstance(accounts, list)
    ):
        return {}
    availability = {}
    credential_ids = set()
    for account in accounts:
        if not isinstance(account, dict):
            return {}
        credential_id = account.get("credentialId")
        label = account.get("label")
        if (
            type(credential_id) is not int
            or credential_id < 0
            or not isinstance(label, str)
            or not label.strip()
            or label in availability
            or credential_id in credential_ids
        ):
            return {}
        credential_ids.add(credential_id)
        availability[label] = "blocked" if str(credential_id) in owned else "open"
    return availability


def _read_codex_rotation() -> dict | None:
    """Read omp's rotation status without changing its state or mode."""
    directory = _expand("~/.omp/agent/codex-rotation")
    try:
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict):
            return None
        mode = state.get("mode")
        stalled = state.get("stalled", False)
        if (
            not isinstance(mode, str)
            or mode not in CODEX_ROTATION_DESCRIPTIONS
            or not isinstance(stalled, bool)
        ):
            return None
        try:
            with open(os.path.join(directory, "mode.json"), encoding="utf-8") as stream:
                config = json.load(stream)
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            return None
        operating_mode = config.get("operatingMode", "confirm")
        if operating_mode not in ("auto", "confirm"):
            return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    result = {
        "mode": mode,
        "description": CODEX_ROTATION_DESCRIPTIONS[mode],
        "operatingMode": operating_mode,
        "stalled": stalled,
    }
    accounts = _codex_rotation_accounts(state)
    if accounts:
        result["accountAvailability"] = accounts
    return result


def _result_error(provider: str, code: str, message: str, source: str | None = None) -> dict:
    return {
        "id": provider,
        "ok": False,
        "source": source,
        "error": {"code": code, "message": message},
    }


# Money as the balance details rows format it, e.g. "$6.17" or "$1,234.50".
_OR_MONEY_RE = re.compile(r"^\s*\$?([\d,]+(?:\.\d+)?)\s*$")
# Identity line the CLI builds when it has a credit balance, e.g. "Balance: $6.17".
_OR_BALANCE_RE = re.compile(r"^\s*Balance:\s*\$?([\d,]+(?:\.\d+)?)\s*$")


def _balance_text(usage: dict) -> str | None:
    """Remaining credit of a balance-only provider as "$6.17 left".

    OpenRouter: CLI 0.56.3 stopped emitting `openRouterUsage`, so the remaining
    credit is only in the "Credits" / "Remaining" details row, with the identity
    line ("Balance: $6.17") as the last resort. TypeSafe only has the identity
    line. Neither carries a numeric field. Returns None when no amount parses.
    """
    details = usage.get("details")
    for section in details if isinstance(details, list) else []:
        if not isinstance(section, dict) or section.get("title") != "Credits":
            continue
        rows = section.get("rows")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("label") != "Remaining":
                continue
            m = _OR_MONEY_RE.match(str(row.get("value") or ""))
            if m:
                return f"${float(m.group(1).replace(',', '')):.2f} left"
    m = _OR_BALANCE_RE.match(str(usage.get("loginMethod") or ""))
    if m:
        return f"${float(m.group(1).replace(',', '')):.2f} left"
    return None


def _reset_credits(usage: dict) -> dict | None:
    """Codex "saved reset" credits: how many are usable and which expires first.

    The CLI mirrors the OpenAI payload (`codexResetCredits.credits[]` with a
    `status` and `expires_at`). Only `available` credits count; the soonest
    expiry is the one the user has to spend first. None when absent.
    """
    raw = usage.get("codexResetCredits")
    if not isinstance(raw, dict):
        return None
    credits = raw.get("credits")
    soonest: _dt.datetime | None = None
    count = 0
    for credit in credits if isinstance(credits, list) else []:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        count += 1
        expires = _parse_iso(credit.get("expires_at"))
        if expires is not None and (soonest is None or expires < soonest):
            soonest = expires
    return {
        "count": count,
        "soonestExpiresAt": soonest.isoformat() if soonest else None,
    }


# Claude "saved resets" ship in the `cedar_ember` block of Anthropic's OAuth
# usage endpoint. The CodexBar CLI (0.64.1) drops the block, so it is read
# directly. The server only answers for Claude Code's client identity; any
# other User-Agent gets `ineligible_reason: "surface"` and no grants.
CLAUDE_CREDENTIALS_PATH = "~/.claude/.credentials.json"
CLAUDE_RESET_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
CLAUDE_RESET_USER_AGENT = "claude-cli/2.1.280 (external, cli)"
CLAUDE_RESET_TIMEOUT = 8.0


def _claude_reset_credits(status, now: _dt.datetime) -> dict | None:
    """Usable Claude reset grants in the Codex `resetCredits` shape.

    Counts `resets_left` of every grant that is not paused and whose
    `starts_at`..`ends_at` window contains now. None when ineligible.
    """
    if not isinstance(status, dict) or status.get("eligible") is not True:
        return None
    grants = status.get("grants")
    soonest: _dt.datetime | None = None
    count = 0
    for grant in grants if isinstance(grants, list) else []:
        if not isinstance(grant, dict) or grant.get("paused") is True:
            continue
        left = grant.get("resets_left")
        if isinstance(left, bool) or not isinstance(left, int) or left < 1:
            continue
        starts = _parse_iso(grant.get("starts_at"))
        ends = _parse_iso(grant.get("ends_at"))
        if (starts is not None and starts > now) or (ends is not None and ends <= now):
            continue
        count += left
        if ends is not None and (soonest is None or ends < soonest):
            soonest = ends
    return {
        "count": count,
        "soonestExpiresAt": soonest.isoformat() if soonest else None,
    }


def _fetch_claude_reset_credits(timeout: float) -> dict | None:
    """Best effort: missing or expired credentials, or any failure, yield None."""
    try:
        with open(_expand(CLAUDE_CREDENTIALS_PATH), encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        expires_ms = _as_float(oauth.get("expiresAt"))
        if not isinstance(token, str) or not token:
            return None
        if expires_ms is not None and expires_ms <= time.time() * 1000:
            return None
        body = _read_http_body(CLAUDE_RESET_URL, timeout, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLAUDE_RESET_USER_AGENT,
        })
        data = json.loads(body.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            return None
        return _claude_reset_credits(
            data.get("cedar_ember"), _dt.datetime.now(_dt.timezone.utc)
        )
    except Exception:  # noqa: BLE001 - the endpoint cannot break usage
        return None



def _clamp_to_account_reset(primary, secondary):
    """End the session window at an earlier weekly reset.

    The weekly reset also resets the session window, so a 5h window whose
    natural end falls after it actually ends at the weekly reset. The copy
    keeps the natural start in `startsAt` so pace spans the truncated window.
    """
    if not isinstance(primary, dict) or not isinstance(secondary, dict):
        return primary
    end = _parse_iso(primary.get("resetsAt"))
    weekly = _parse_iso(secondary.get("resetsAt"))
    minutes = primary.get("windowMinutes")
    if end is None or weekly is None or weekly >= end:
        return primary
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return primary
    start = end - _dt.timedelta(minutes=minutes)
    if weekly <= start:
        return primary
    return {
        **primary,
        "resetsAt": secondary["resetsAt"],
        "startsAt": start.isoformat().replace("+00:00", "Z"),
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
    if provider == "openrouter":
        # The CodexBar CLI returns primary.usedPercent=100 as a placeholder.
        # Only a real per-key allowance replaces it; otherwise no bar renders.
        primary = None
        if isinstance(or_usage, dict):
            key_limit = or_usage.get("keyLimit")
            if isinstance(key_limit, (int, float)) and key_limit > 0:
                monthly = or_usage.get("keyUsageMonthly")
                monthly = monthly if isinstance(monthly, (int, float)) else 0.0
                primary = {
                    "usedPercent": min(100.0, (monthly / key_limit) * 100.0),
                    "resetDescription": f"${monthly:.2f} / ${key_limit:.0f}",
                }
        if primary is None:
            balance_text = _balance_text(usage)
    elif provider == "typesafe":
        # Spend and credit balance only; the CLI emits no rate window.
        primary = None
        balance_text = _balance_text(usage)
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
    secondary = usage.get("secondary")
    return {
        "id": provider,
        "ok": True,
        "source": record.get("source"),
        "identity": identity,
        "loginMethod": login_method,
        "accountEmail": account_email,
        "primary": _clamp_to_account_reset(primary, secondary),
        "secondary": secondary,
        "tertiary": usage.get("tertiary"),
        "extraRateWindows": extra_rate_windows,
        "resetCredits": _reset_credits(usage) if provider == "codex" else None,
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
    except OSError as exc:
        return [_result_error(provider, "cli_error", str(exc), source)]
    except UnicodeError as exc:
        return [_result_error(provider, "parse", str(exc), source)]

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
    if not any(result.get("ok") for result in results):
        for fallback in PROVIDER_FALLBACK_SOURCES.get(provider, []):
            retries = _run_cli(cli, provider, fallback, timeout)
            if any(retry.get("ok") for retry in retries):
                results = retries
                break
    if provider == "claude" and any(result.get("ok") for result in results):
        credits = _fetch_claude_reset_credits(CLAUDE_RESET_TIMEOUT)
        for result in results:
            if result.get("ok"):
                result["resetCredits"] = credits
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
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?", proc.stdout or "")
    return match.group(0) if match else None

# codex-reset.com publishes a probabilistic forecast for the next OpenAI
# usage-limit reset. It exposes no point estimate, only per-horizon
# probabilities plus the observed cadence, so the ETA below is derived:
# last confirmed reset + recent median cadence, snapped into the hour window
# resets historically land in.
FORECAST_TIMEOUT = 8.0
FORECAST_MAX_BODY_BYTES = 256 * 1024
# Resets happen every few days and the model refreshes slowly, so a short
# cache keeps a 30-second poll tick from hammering a third-party endpoint.
FORECAST_CACHE_TTL = 900.0
FORECAST_CACHE_PATH = "~/.codexbar/forecast_cache.json"
FORECAST_MAX_MEDIAN_DAYS = 365.0
# Same origin as the forecast. Open Codex incidents precede compensation
# resets, so the status feed rides along with the forecast fetch.
FORECAST_STATUS_PATH = "/api/status-history"
FORECAST_STATUS_TIMEOUT = 4.0


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
    # Announcements outlive the reset they promise when it is delivered as a
    # banked reset, which never moves `last_reset_at`. Once one lapses, its
    # post also settles every earlier alert and hint.
    cutoff = last_reset
    for key, at_key in (("official_signal", "at"), ("latest_alert", "source_at")):
        raw = data.get(key)
        if isinstance(raw, dict) and _announcement_lapsed(raw, now):
            at = _parse_iso(raw.get(at_key))
            if at is not None and at > cutoff:
                cutoff = at
    # The latest alert only explains the *next* reset when it postdates the
    # cutoff; otherwise it announced a reset already recorded or settled.
    alert = data.get("latest_alert")
    alert_summary = None
    if isinstance(alert, dict):
        alert_at = _parse_iso(alert.get("source_at"))
        summary = alert.get("summary")
        if (
            alert_at is not None
            and alert_at > cutoff
            and isinstance(summary, str)
            and summary.strip()
        ):
            alert_summary = summary.strip()
    teased = data.get("teased_window")
    signal = _forecast_signal(data, cutoff, now)
    hint = None
    raw_hint = data.get("latest_hint")
    if isinstance(raw_hint, dict):
        hint_at = _parse_iso(raw_hint.get("at"))
        quote = raw_hint.get("quote")
        if hint_at is not None and hint_at > cutoff and isinstance(quote, str) and quote.strip():
            hint = {
                "at": hint_at.isoformat(),
                "quote": quote.strip(),
                "url": str(raw_hint.get("url") or "") or None,
            }
    wait = None
    raw_wait = _obj("wait_comparison")
    wait_days = _as_float(raw_wait.get("wait_days"))
    if wait_days is not None and wait_days >= 0:
        share = _as_float(raw_wait.get("shorter_share"))
        wait = {
            "days": wait_days,
            "shorterShare": share if share is not None and 0 <= share <= 1 else None,
            "sample": _as_float(raw_wait.get("sample")),
            "medianDays": _as_float(raw_wait.get("median_days")),
            "longestDays": _as_float(raw_wait.get("longest_days")),
        }
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
        "signal": signal,
        "hint": hint,
        "wait": wait,
        "incident": None,
        "prob24h": _percent("rounded_24h", "raw_24h"),
        "prob48h": _percent("rounded_48h", "raw_48h"),
        "confidence": str(data.get("confidence") or ""),
        "lastResetAt": last_reset.isoformat() if last_reset is not None else None,
        "alertSummary": alert_summary,
        "medianDays": median_days,
        "error": None,
    }


def _announcement_window(raw: dict) -> tuple[dict, _dt.datetime | None]:
    window = raw.get("window")
    window = window if isinstance(window, dict) else {}
    deadline = _parse_iso(window.get("target_at")) or _parse_iso(window.get("end_at"))
    return window, deadline


def _announcement_lapsed(raw: dict, now: _dt.datetime) -> bool:
    _, deadline = _announcement_window(raw)
    return raw.get("state") == "expired" or (deadline is not None and deadline <= now)


def _forecast_signal(
    data: dict, cutoff: _dt.datetime, now: _dt.datetime
) -> dict | None:
    """An announced reset (e.g. a dated commitment on X) that postdates the
    cutoff and whose window is still open. `official_signal` is the scored
    announcement; `latest_alert` carries the same window when the site has
    only an alert. None in plain model mode or once the announcement lapsed.
    """
    for key, at_key in (("official_signal", "at"), ("latest_alert", "source_at")):
        raw = data.get(key)
        if not isinstance(raw, dict) or _announcement_lapsed(raw, now):
            continue
        at = _parse_iso(raw.get(at_key))
        if at is None or at <= cutoff:
            continue
        window, deadline = _announcement_window(raw)
        score = raw.get("score")
        percent = None
        if isinstance(score, dict):
            percent = _as_float(score.get("value"))
        elif score is not None:
            percent = _as_float(score)
        if percent is None:
            probs = data.get("probabilities")
            if isinstance(probs, dict):
                percent = _as_float(probs.get("signal_percent"))
        if percent is not None and not 0 <= percent <= 100:
            percent = None
        label = window.get("label")
        summary = raw.get("summary")
        return {
            "percent": percent,
            "band": str(raw.get("signal_type") or data.get("signal_tier") or ""),
            "windowLabel": label.strip() if isinstance(label, str) and label.strip() else None,
            "deadlineAt": deadline.isoformat() if deadline is not None else None,
            "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
            "url": str(raw.get("url") or "") or None,
            "at": at.isoformat(),
        }
    return None


def _http_worker() -> int:
    """Private HTTP worker: request on stdin, bounded body on stdout."""
    try:
        request_data = json.load(sys.stdin)
        headers = {"User-Agent": "codexbar-kde", "Accept": "application/json"}
        headers.update(request_data.get("headers") or {})
        request = urllib.request.Request(request_data["url"], headers=headers)
        with urllib.request.urlopen(
            request, timeout=request_data["timeout"]
        ) as response:
            body = response.read(FORECAST_MAX_BODY_BYTES + 1)
        if len(body) > FORECAST_MAX_BODY_BYTES:
            sys.stdout.write("response exceeds the body size limit")
            return 1
        sys.stdout.buffer.write(body)
        return 0
    except Exception:  # noqa: BLE001 - never echo URLs or credentials
        sys.stdout.write("request failed")
        return 1


def _read_http_body(url: str, timeout: float, headers: dict | None = None) -> bytes:
    # Socket timeouts cannot stop a slow-drip response or bound DNS lookup.
    # Exec a disposable worker, including startup in its wall-clock budget.
    # Headers travel over stdin so credentials never reach argv.
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--http-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            body, _ = proc.communicate(
                json.dumps(
                    {"url": url, "timeout": timeout, "headers": headers or {}}
                ).encode("utf-8"),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
    if proc.returncode != 0:
        raise ValueError(body.decode("utf-8", "replace") or "http worker failed")
    return body


def _forecast_failure(exc: Exception) -> dict:
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


def _normalize_incident(data: dict) -> dict | None:
    """Open Codex incident from codex-reset.com's status feed, else None."""
    current = data.get("current")
    if not isinstance(current, dict):
        return None
    active = current.get("active_incident")
    codex_status = str(current.get("codex") or "")
    open_incident = (
        isinstance(active, dict)
        or current.get("degraded") is True
        or (codex_status != "" and codex_status != "operational")
    )
    if not open_incident:
        return None
    surfaces = current.get("surfaces")
    degraded = [
        str(surface.get("label") or surface.get("id") or "")
        for surface in (surfaces if isinstance(surfaces, list) else [])
        if isinstance(surface, dict) and surface.get("status") not in (None, "operational")
    ]
    name = active.get("name") if isinstance(active, dict) else None
    return {
        "open": True,
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
        "codexStatus": codex_status or None,
        "surfaces": [label for label in degraded if label],
    }


def _fetch_incident(forecast_url: str, timeout: float) -> dict | None:
    """Status feed is best effort: any failure just means no incident line."""
    try:
        parts = urllib.parse.urlsplit(forecast_url)
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, FORECAST_STATUS_PATH, "", "")
        )
        body = _read_http_body(url, timeout).decode("utf-8", "replace").strip()
        data = json.loads(body) if body else None
        return _normalize_incident(data) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - the endpoint cannot break usage
        return None


def _fetch_forecast(url: str, timeout: float) -> dict:
    """Fetch and normalize the reset forecast without raising."""
    try:
        fresh = _forecast_cache_read(FORECAST_CACHE_TTL)
        if fresh is not None:
            return fresh
        body = _read_http_body(url, timeout).decode("utf-8", "replace").strip()
        if not body:
            raise ValueError("empty response body")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        payload = _normalize_forecast(data)
    except Exception as exc:  # noqa: BLE001 - the endpoint cannot break usage
        return _forecast_failure(exc)
    payload["incident"] = _fetch_incident(url, min(timeout, FORECAST_STATUS_TIMEOUT))
    _forecast_cache_write(payload)
    return payload


def _positive_timeout(value: str) -> float:
    timeout = _as_float(value)
    if timeout is None or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return timeout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli-path", required=True)
    parser.add_argument(
        "--providers",
        default="codex,claude,zai,opencodego,openrouter,kilo,typesafe",
        help="Comma-separated provider ids to query.",
    )
    parser.add_argument("--timeout", type=_positive_timeout, default=30.0)
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
            "forecast": None,
            "codexRotation": None,
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
            try:
                results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - isolate provider failures
                provider = futures[fut]
                results.append(_result_error(provider, "provider", str(exc)))

    forecast = None
    if forecast_future is not None:
        try:
            forecast = forecast_future.result()
        except Exception as exc:  # noqa: BLE001 - keep healthy provider results
            forecast = _forecast_failure(exc)

    # Preserve the requested provider order in output.
    order = {p: i for i, p in enumerate(providers)}
    results.sort(key=lambda r: order.get(r["id"], 999))

    out = {
        "updatedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "providers": results,
        "fatal": None,
        "forecast": forecast,
        "codexRotation": _read_codex_rotation() if "codex" in providers else None,
        "cliVersion": _cli_version(cli),
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        sys.exit(_http_worker())
    sys.exit(main(sys.argv[1:]))
