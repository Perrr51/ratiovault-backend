"""Tests for `supabase_client` singleton and import guard."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROUTERS_DIR = Path(__file__).parent.parent / "routers"
ALLOWED = {"webhooks.py", "checkout.py", "portal.py", "internal.py"}


def test_service_client_is_singleton(monkeypatch):
    """`get_supabase_service()` must return the exact same instance on every call."""
    import supabase_client

    # Ensure any prior cached value is cleared before we patch create_client.
    supabase_client.get_supabase_service.cache_clear()

    sentinel = object()
    calls = {"n": 0}

    def fake_create_client(url, key):
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(supabase_client, "create_client", fake_create_client)
    monkeypatch.setattr(supabase_client.settings, "supabase_url", "http://localhost:54321")
    monkeypatch.setattr(supabase_client.settings, "supabase_service_role_key", "test-key")

    a = supabase_client.get_supabase_service()
    b = supabase_client.get_supabase_service()

    assert a is b
    assert a is sentinel
    assert calls["n"] == 1, "create_client must only be invoked once due to lru_cache"

    # Clean up cache so other tests (or repeated runs) start fresh.
    supabase_client.get_supabase_service.cache_clear()


def test_service_import_is_restricted():
    """Only allow-listed routers may import `get_supabase_service`."""
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        if path.name in ALLOWED or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "supabase_client":
                names = {a.name for a in node.names}
                assert "get_supabase_service" not in names, (
                    f"{path.name} imports get_supabase_service (not in ALLOWED)"
                )
            elif isinstance(node, ast.Import):
                for a in node.names:
                    # Flag bare `import supabase_client` as suspicious too.
                    assert a.name != "supabase_client", (
                        f"{path.name} imports supabase_client module (not in ALLOWED)"
                    )
