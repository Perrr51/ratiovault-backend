"""Test fixtures for `api/` tests.

Provides `supabase_local`: a session-scoped fixture that boots a local Supabase
stack via `npx supabase start`, runs `npx supabase db reset` to apply all
migrations into a clean DB, and yields a service_role `supabase-py` client
pointing at the local REST endpoint (http://localhost:54321).

If the stack was already running when the test session started, we reuse it
and do NOT stop it on teardown (to speed up dev loops). If the stack was
booted by this process, we run `npx supabase stop` on teardown.

Set env var `SKIP_SUPABASE_LOCAL=1` to skip all tests that depend on this
fixture (useful in CI without Docker).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

# portfolio-tracker root (one level above `api/`) — where `supabase/config.toml` lives.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], *, capture: bool = True, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
    )


def _status_json() -> dict | None:
    """Return parsed `supabase status -o json` or None if the stack is down."""
    try:
        res = _run(["npx", "--yes", "supabase", "status", "-o", "json"], check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _parse_start_output(stdout: str) -> dict[str, str]:
    """Parse plaintext output of `supabase start` into a dict of keys → values."""
    fields: dict[str, str] = {}
    # lines look like: "         API URL: http://127.0.0.1:54321"
    for line in stdout.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_ ]+?):\s+(\S.*)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


@pytest.fixture(scope="session")
def supabase_local():
    """Yield a service_role Supabase client against the local CLI stack."""
    if os.environ.get("SKIP_SUPABASE_LOCAL") == "1":
        pytest.skip("SKIP_SUPABASE_LOCAL=1 — skipping local Supabase fixture")

    # Verify supabase CLI is callable.
    try:
        _run(["npx", "--yes", "supabase", "--version"], timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"npx supabase CLI not available: {e}")

    started_by_us = False
    status = _status_json()
    if status is None:
        # Stack is not running — boot it.
        proc = _run(
            ["npx", "--yes", "supabase", "start"],
            timeout=900,  # first boot can pull several images
            check=False,
        )
        if proc.returncode != 0:
            pytest.fail(
                "`npx supabase start` failed:\n"
                f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
            )
        started_by_us = True
        status = _status_json()
        if status is None:
            # Fall back to parsing start output.
            status = _parse_start_output(proc.stdout)

    # Extract API URL + service_role key across both json and plaintext shapes.
    api_url = status.get("API_URL") or status.get("api_url") or status.get("API URL")
    service_key = (
        status.get("SERVICE_ROLE_KEY")
        or status.get("service_role_key")
        or status.get("service_role key")
    )
    if not api_url or not service_key:
        pytest.fail(f"Could not determine API URL / service_role key from status: {status!r}")

    # Reset DB to apply migrations cleanly.
    # Note: `supabase db reset` finishes by health-checking every container
    # (including storage). On slower machines the storage HTTP probe times out
    # even though migrations have been applied successfully. We treat the run
    # as OK if the migrations phase ran and only the post-reset storage probe
    # failed, since our tests only touch Postgres via PostgREST.
    reset = _run(
        ["npx", "--yes", "supabase", "db", "reset", "--local"],
        timeout=600,
        check=False,
    )
    migrations_ok = "Applying migration" in reset.stderr or "Applying migration" in reset.stdout
    benign_storage_probe = (
        "127.0.0.1:54321/storage" in reset.stderr
        and "context deadline exceeded" in reset.stderr
    )
    if reset.returncode != 0 and not (migrations_ok and benign_storage_probe):
        pytest.fail(
            "`npx supabase db reset --local` failed:\n"
            f"STDOUT:\n{reset.stdout}\n\nSTDERR:\n{reset.stderr}"
        )

    try:
        from supabase import create_client
    except ImportError as e:
        pytest.fail(f"supabase-py not installed: {e}")

    client = create_client(api_url, service_key)

    yield client

    if started_by_us:
        _run(["npx", "--yes", "supabase", "stop"], check=False, timeout=120)
