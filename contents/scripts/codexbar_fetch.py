#!/usr/bin/env python3
"""CodexBar KDE plasmoid data fetcher.

Runs `codexbar usage --json --provider <id> [--source <s>]` for each enabled
provider in parallel, merges the per-provider results into a single JSON
document on stdout. The QML widget calls this once per polling tick.

Usage:
  codexbar_fetch.py --cli-path PATH --providers codex,claude,openrouter,kilo
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
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


def _run_cli(cli: str, provider: str, source: str | None, timeout: float) -> dict:
    """Invoke `codexbar usage` for one provider. Returns the inner usage record."""
    cmd = [cli, "usage", "--json", "--provider", provider]
    if source:
        cmd += ["--source", source]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return {
            "id": provider,
            "ok": False,
            "error": {"code": "cli_missing", "message": f"CLI not found at {cli}"},
        }
    except subprocess.TimeoutExpired:
        return {
            "id": provider,
            "ok": False,
            "error": {"code": "timeout", "message": f"CLI timed out after {timeout}s"},
        }
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {
            "id": provider,
            "ok": False,
            "error": {
                "code": "no_output",
                "message": (proc.stderr or "empty stdout").strip()[:400],
            },
        }
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "id": provider,
            "ok": False,
            "error": {"code": "parse", "message": f"{exc}: {stdout[:200]}"},
        }
    record = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(record, dict):
        return {
            "id": provider,
            "ok": False,
            "error": {"code": "shape", "message": "unexpected CLI payload"},
        }
    if "error" in record and record.get("error"):
        err = record["error"]
        return {
            "id": provider,
            "ok": False,
            "source": record.get("source"),
            "error": {
                "code": str(err.get("kind", "provider")),
                "message": str(err.get("message", "unknown error")),
            },
        }
    usage = record.get("usage") or {}
    primary = usage.get("primary")
    or_usage = usage.get("openRouterUsage")
    balance_text: str | None = None
    if provider == "openrouter" and or_usage and isinstance(or_usage, dict):
        # The CodexBar CLI returns primary.usedPercent=100 as a placeholder.
        # We replace it with a meaningful per-key allowance bar when the user
        # has set a credit limit on this key; otherwise we drop primary
        # entirely so the section just shows the balance in its header.
        key_limit = or_usage.get("keyLimit")
        if isinstance(key_limit, (int, float)) and key_limit > 0:
            monthly = or_usage.get("keyUsageMonthly")
            monthly = monthly if isinstance(monthly, (int, float)) else 0.0
            pct = min(100.0, (monthly / key_limit) * 100.0) if key_limit > 0 else 0.0
            primary = {
                "usedPercent": pct,
                "resetDescription": f"${monthly:.2f} / ${key_limit:.0f}",
            }
        else:
            primary = None
    elif provider == "kilo" and isinstance(primary, dict):
        # CLI returns `primary.resetDescription = "6.62/20 credits"`. With
        # auto-topup off there's no recurring cap — just a prepaid balance —
        # so mirror the openrouter "no keyLimit" case: drop the bar and surface
        # remaining credits in the header instead.
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
    return {
        "id": provider,
        "ok": True,
        "source": record.get("source"),
        "identity": usage.get("identity") or {},
        "loginMethod": usage.get("loginMethod"),
        "accountEmail": usage.get("accountEmail"),
        "primary": primary,
        "secondary": usage.get("secondary"),
        "tertiary": usage.get("tertiary"),
        # Extra named windows like Claude Design + Daily Routines come through
        # the OAuth source only — CLI fallback leaves this empty.
        "extraRateWindows": usage.get("extraRateWindows") or [],
        "openRouterUsage": or_usage,
        "balanceText": balance_text,
        "updatedAt": usage.get("updatedAt"),
        "error": None,
    }


def _fetch_provider(cli: str, provider: str, timeout: float) -> dict:
    primary_source = PROVIDER_SOURCE.get(provider)
    result = _run_cli(cli, provider, primary_source, timeout)
    if result.get("ok"):
        return result
    for fallback in PROVIDER_FALLBACK_SOURCES.get(provider, []):
        retry = _run_cli(cli, provider, fallback, timeout)
        if retry.get("ok"):
            return retry
    return result


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli-path", required=True)
    parser.add_argument(
        "--providers",
        default="codex,claude,openrouter,kilo",
        help="Comma-separated provider ids to query.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
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
        }
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        futures = {
            pool.submit(_fetch_provider, cli, p, args.timeout): p for p in providers
        }
        for fut in as_completed(futures):
            results.append(fut.result())

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
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
