"""Tests for services/snapshot_cache.py — per-user TTL memoization.

TDD RED phase: C1–C9 exercising miss/hit/expiry/per-user-isolation/
per-account-isolation/invalidate/clear/lock-smoke/now_fn-injection.
All assertions call production code and verify specific expected values.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supa():
    return MagicMock()


def _fake_snapshot(value: str = "snap") -> dict:
    """A minimal snapshot dict — distinctive value aids identity checks."""
    return {"total": 1000.0, "_id": value}


# ---------------------------------------------------------------------------
# C1 — Cache miss triggers computation and storage
# ---------------------------------------------------------------------------

def test_miss_triggers_compute_and_store():
    """Cache miss: get_vault_snapshot called once; result stored and returned."""
    import services.snapshot_cache as sc
    sc.clear()

    expected = _fake_snapshot("c1")
    with patch("services.snapshot_cache.get_vault_snapshot", return_value=expected) as mock_fn:
        supa = _make_supa()
        result = sc.get_cached_snapshot(supa, "user-1")

    assert result is expected
    mock_fn.assert_called_once()
    # Entry must be stored
    assert sc.get("user-1") is expected


# ---------------------------------------------------------------------------
# C2 — Cache hit returns stored value without recomputing
# ---------------------------------------------------------------------------

def test_hit_returns_without_recomputing():
    """Cache hit: second call does NOT invoke get_vault_snapshot."""
    import services.snapshot_cache as sc
    sc.clear()

    first_snap = _fake_snapshot("c2-first")
    with patch("services.snapshot_cache.get_vault_snapshot", return_value=first_snap) as mock_fn:
        supa = _make_supa()
        result1 = sc.get_cached_snapshot(supa, "user-2")
        result2 = sc.get_cached_snapshot(supa, "user-2")

    assert result1 is first_snap
    assert result2 is first_snap
    assert mock_fn.call_count == 1  # second call must NOT recompute


# ---------------------------------------------------------------------------
# C3 — Expired entry triggers recomputation
# ---------------------------------------------------------------------------

def test_expired_entry_recomputed():
    """Expired entry: now_fn simulates t+91 → recomputation happens."""
    import services.snapshot_cache as sc
    sc.clear()

    snap_v1 = _fake_snapshot("c3-v1")
    snap_v2 = _fake_snapshot("c3-v2")
    tick = [0.0]

    def monotonic():
        return tick[0]

    with patch("services.snapshot_cache.get_vault_snapshot", side_effect=[snap_v1, snap_v2]) as mock_fn:
        supa = _make_supa()
        # First call at t=0 → miss → compute snap_v1
        r1 = sc.get_cached_snapshot(supa, "user-3", now_fn=monotonic)
        # Advance time past TTL
        tick[0] = 91.0
        # Second call at t=91 → expired → compute snap_v2
        r2 = sc.get_cached_snapshot(supa, "user-3", now_fn=monotonic)

    assert r1 is snap_v1
    assert r2 is snap_v2
    assert mock_fn.call_count == 2


# ---------------------------------------------------------------------------
# C4 — Per-user isolation
# ---------------------------------------------------------------------------

def test_per_user_isolation():
    """User A cache does NOT satisfy a miss for user B."""
    import services.snapshot_cache as sc
    sc.clear()

    snap_a = _fake_snapshot("c4-a")
    snap_b = _fake_snapshot("c4-b")

    with patch("services.snapshot_cache.get_vault_snapshot", side_effect=[snap_a, snap_b]) as mock_fn:
        supa = _make_supa()
        sc.get_cached_snapshot(supa, "user-a")
        result_b = sc.get_cached_snapshot(supa, "user-b")

    assert result_b is snap_b
    assert mock_fn.call_count == 2
    # User A entry still intact
    assert sc.get("user-a") is snap_a


# ---------------------------------------------------------------------------
# C5 — Per-account isolation
# ---------------------------------------------------------------------------

def test_per_account_isolation():
    """(uid, None) and (uid, 'acc-x') are separate cache entries."""
    import services.snapshot_cache as sc
    sc.clear()

    snap_all = _fake_snapshot("c5-all")
    snap_acc = _fake_snapshot("c5-acc")

    with patch("services.snapshot_cache.get_vault_snapshot", side_effect=[snap_all, snap_acc]) as mock_fn:
        supa = _make_supa()
        r_all = sc.get_cached_snapshot(supa, "user-5")
        r_acc = sc.get_cached_snapshot(supa, "user-5", account_id="acc-x")

    assert r_all is snap_all
    assert r_acc is snap_acc
    assert mock_fn.call_count == 2
    # Both entries coexist
    assert sc.get("user-5", None) is snap_all
    assert sc.get("user-5", "acc-x") is snap_acc


# ---------------------------------------------------------------------------
# C6 — Invalidate drops a single entry
# ---------------------------------------------------------------------------

def test_invalidate_drops_entry():
    """After invalidate, next get_cached_snapshot recomputes for that key only."""
    import services.snapshot_cache as sc
    sc.clear()

    snap_v1 = _fake_snapshot("c6-v1")
    snap_v2 = _fake_snapshot("c6-v2")

    with patch("services.snapshot_cache.get_vault_snapshot", side_effect=[snap_v1, snap_v2]) as mock_fn:
        supa = _make_supa()
        sc.get_cached_snapshot(supa, "user-6")
        sc.invalidate("user-6")
        r2 = sc.get_cached_snapshot(supa, "user-6")

    assert r2 is snap_v2
    assert mock_fn.call_count == 2


# ---------------------------------------------------------------------------
# C7 — clear() drops all entries
# ---------------------------------------------------------------------------

def test_clear_drops_all():
    """clear() empties the entire cache regardless of how many entries exist."""
    import services.snapshot_cache as sc
    sc.clear()

    snaps = [_fake_snapshot(f"c7-{i}") for i in range(3)]
    with patch("services.snapshot_cache.get_vault_snapshot", side_effect=snaps):
        supa = _make_supa()
        sc.get_cached_snapshot(supa, "user-7a")
        sc.get_cached_snapshot(supa, "user-7b")
        sc.get_cached_snapshot(supa, "user-7c")

    sc.clear()
    # After clear, every key is a miss
    assert sc.get("user-7a") is None
    assert sc.get("user-7b") is None
    assert sc.get("user-7c") is None


# ---------------------------------------------------------------------------
# C8 — Lock does not deadlock on sequential get/set/invalidate
# ---------------------------------------------------------------------------

def test_lock_does_not_deadlock():
    """Sequential get/set/invalidate all return without hanging (smoke test)."""
    import services.snapshot_cache as sc
    sc.clear()

    supa = _make_supa()
    snap = _fake_snapshot("c8")

    with patch("services.snapshot_cache.get_vault_snapshot", return_value=snap):
        r1 = sc.get_cached_snapshot(supa, "user-8")

    sc.invalidate("user-8")
    sc.clear()
    # All calls returned — no deadlock
    assert r1 is snap


# ---------------------------------------------------------------------------
# C9 — now_fn injection: expires_at = fixed + 90
# ---------------------------------------------------------------------------

def test_now_fn_injection_works():
    """Custom now_fn → expires_at stored as now_fn() + 90 (TTL precision)."""
    import services.snapshot_cache as sc
    sc.clear()

    snap = _fake_snapshot("c9")
    fixed_time = 500.0

    with patch("services.snapshot_cache.get_vault_snapshot", return_value=snap):
        supa = _make_supa()
        sc.get_cached_snapshot(supa, "user-9", now_fn=lambda: fixed_time)

    # The entry should still be fresh at fixed_time + 89 but expired at fixed_time + 91
    assert sc.get("user-9", now_fn=lambda: fixed_time + 89.0) is snap
    assert sc.get("user-9", now_fn=lambda: fixed_time + 91.0) is None
