from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .models import SourceRecord, utc_now

SCHEMA_VERSION = 1


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                record_key TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                format TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key TEXT NOT NULL,
                candidate_index INTEGER NOT NULL,
                outcome_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(record_key) REFERENCES items(record_key)
            );
            CREATE INDEX IF NOT EXISTS attempts_record_key
                ON attempts(record_key);
            CREATE TABLE IF NOT EXISTS robots_cache (
                origin TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status_code INTEGER,
                policy TEXT NOT NULL,
                body TEXT,
                error TEXT
            );
            """
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError("Unsupported downloader state schema")
        self.connection.commit()

    def register(self, record: SourceRecord, ordinal: int) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO items(
                record_key, ordinal, record_id, format, status, updated_at
            ) VALUES(?, ?, ?, ?, 'pending', ?)
            """,
            (record.key, ordinal, record.record_id, record.format, utc_now()),
        )
        self.connection.commit()

    def should_skip(self, record: SourceRecord, *, retry_failures: bool) -> bool:
        row = self.connection.execute(
            "SELECT status FROM items WHERE record_key = ?",
            (record.key,),
        ).fetchone()
        if row is None or row["status"] in {"pending", "in_progress"}:
            return False
        return not (retry_failures and row["status"] != "downloaded")

    def mark_in_progress(self, record: SourceRecord) -> None:
        self.connection.execute(
            """
            UPDATE items
            SET status = 'in_progress', updated_at = ?
            WHERE record_key = ?
            """,
            (utc_now(), record.key),
        )
        self.connection.commit()

    def add_attempt(
        self,
        record: SourceRecord,
        candidate_index: int,
        outcome: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO attempts(
                record_key, candidate_index, outcome_json, created_at
            ) VALUES(?, ?, ?, ?)
            """,
            (
                record.key,
                candidate_index,
                json.dumps(outcome, sort_keys=True),
                utc_now(),
            ),
        )
        self.connection.commit()

    def complete(
        self,
        record: SourceRecord,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            UPDATE items
            SET status = ?, result_json = ?, updated_at = ?
            WHERE record_key = ?
            """,
            (status, json.dumps(result, sort_keys=True), utc_now(), record.key),
        )
        self.connection.commit()

    def get_robots(self, origin: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM robots_cache WHERE origin = ?",
            (origin,),
        ).fetchone()

    def put_robots(
        self,
        *,
        origin: str,
        fetched_at: float,
        expires_at: float,
        status_code: int | None,
        policy: str,
        body: str | None,
        error: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO robots_cache(
                origin, fetched_at, expires_at, status_code, policy, body, error
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at,
                status_code = excluded.status_code,
                policy = excluded.policy,
                body = excluded.body,
                error = excluded.error
            """,
            (origin, fetched_at, expires_at, status_code, policy, body, error),
        )
        self.connection.commit()

    def export_results(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        rows = self.connection.execute(
            """
            SELECT result_json FROM items
            WHERE result_json IS NOT NULL
            ORDER BY ordinal, record_key
            """
        )
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row["result_json"] + "\n")
        os.replace(temporary, path)

    def summary(self) -> dict[str, int]:
        counts = Counter()
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM items GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["count"])
        return dict(sorted(counts.items()))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
