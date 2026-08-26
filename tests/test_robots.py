from __future__ import annotations

import asyncio

from refinedgaw_rehydrate.robots import RobotsFetch, RobotsPolicy
from refinedgaw_rehydrate.state import StateStore


def test_cache_ttl_starts_when_robots_fetch_completes(tmp_path, monkeypatch) -> None:
    clock = [100.0]

    async def fetch(_url: str) -> RobotsFetch:
        clock[0] = 200.0
        return RobotsFetch(status_code=404, body=None)

    monkeypatch.setattr("refinedgaw_rehydrate.robots.time.time", lambda: clock[0])
    with StateStore(tmp_path / "state.sqlite3") as state:
        policy = RobotsPolicy(
            state=state,
            fetcher=fetch,
            user_agent="RefinedGAW-Rehydrate",
            ttl_seconds=3600,
            failure_ttl_seconds=900,
        )

        asyncio.run(policy._refresh("https://example.org"))

        cached = state.get_robots("https://example.org")
        assert cached is not None
        assert cached["fetched_at"] == 200.0
        assert cached["expires_at"] == 3800.0
