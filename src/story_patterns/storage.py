from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    story_count INTEGER NOT NULL CHECK(story_count > 0)
);
CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    title TEXT NOT NULL,
    tradition TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY(story_id, revision)
);
CREATE INDEX IF NOT EXISTS stories_by_tradition ON stories(tradition, story_id, revision);
CREATE TABLE IF NOT EXISTS batch_stories (
    batch_id TEXT NOT NULL REFERENCES import_batches(batch_id) ON DELETE RESTRICT,
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(batch_id, story_id, revision),
    FOREIGN KEY(story_id, revision) REFERENCES stories(story_id, revision) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS batch_stories_by_story ON batch_stories(story_id, revision, batch_id);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    story_revision INTEGER NOT NULL,
    annotator TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    pattern TEXT NOT NULL,
    youngest_outcome TEXT NOT NULL,
    evidence_event_ids TEXT NOT NULL,
    note TEXT NOT NULL,
    digest TEXT NOT NULL,
    UNIQUE(story_id, story_revision, annotator, sequence),
    FOREIGN KEY(story_id, story_revision) REFERENCES stories(story_id, revision)
);
CREATE INDEX IF NOT EXISTS decisions_by_story ON decisions(story_id, story_revision, annotator, sequence);
CREATE TABLE IF NOT EXISTS adjudications (
    adjudication_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    story_revision INTEGER NOT NULL,
    moderator TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    pattern TEXT NOT NULL,
    youngest_outcome TEXT NOT NULL,
    evidence_event_ids TEXT NOT NULL,
    rationale TEXT NOT NULL,
    digest TEXT NOT NULL,
    UNIQUE(story_id, story_revision, sequence),
    FOREIGN KEY(story_id, story_revision) REFERENCES stories(story_id, revision)
);
CREATE INDEX IF NOT EXISTS adjudications_by_story ON adjudications(story_id, story_revision, sequence);
CREATE TABLE IF NOT EXISTS adjudication_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    adjudication_id TEXT NOT NULL UNIQUE
        REFERENCES adjudications(adjudication_id) ON DELETE RESTRICT,
    moderator TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS withdrawals_by_adjudication
    ON adjudication_withdrawals(adjudication_id);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    document TEXT NOT NULL
);
"""


class StoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("a file-backed database is required for durable local reports")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_connection(self):
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def story_document(self, story_id: str, revision: int) -> dict | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT document FROM stories WHERE story_id=? AND revision=?", (story_id, revision)
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def list_stories(self, tradition: str | None = None) -> list[dict]:
        with self.read_connection() as connection:
            if tradition is None:
                rows = connection.execute(
                    "SELECT document FROM stories ORDER BY tradition, story_id, revision"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT document FROM stories WHERE tradition=? ORDER BY story_id, revision",
                    (tradition,),
                ).fetchall()
        return [json.loads(row["document"]) for row in rows]

    def latest_decisions(self, story_id: str, revision: int) -> list[dict]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT d.* FROM decisions d JOIN ("
                "SELECT annotator, MAX(sequence) AS latest FROM decisions "
                "WHERE story_id=? AND story_revision=? GROUP BY annotator"
                ") newest ON d.annotator=newest.annotator AND d.sequence=newest.latest "
                "WHERE d.story_id=? AND d.story_revision=? ORDER BY d.annotator",
                (story_id, revision, story_id, revision),
            ).fetchall()
        return [dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in rows]

    def decision_history(self, story_id: str, revision: int) -> tuple[list[dict], list[dict], list[dict]]:
        with self.read_connection() as connection:
            votes = connection.execute(
                "SELECT * FROM decisions WHERE story_id=? AND story_revision=? ORDER BY annotator, sequence",
                (story_id, revision),
            ).fetchall()
            adjudications = connection.execute(
                "SELECT * FROM adjudications WHERE story_id=? AND story_revision=? ORDER BY sequence",
                (story_id, revision),
            ).fetchall()
            withdrawals = connection.execute(
                "SELECT w.* FROM adjudication_withdrawals w "
                "JOIN adjudications a ON a.adjudication_id=w.adjudication_id "
                "WHERE a.story_id=? AND a.story_revision=? "
                "ORDER BY a.sequence, w.recorded_at, w.withdrawal_id",
                (story_id, revision),
            ).fetchall()
        return (
            [dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in votes],
            [dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in adjudications],
            [dict(row) for row in withdrawals],
        )

    def active_adjudication(self, story_id: str, revision: int) -> dict | None:
        with self.read_connection() as connection:
            record = self._latest_adjudication(connection, story_id, revision)
        if record is None:
            return None
        return record | {"evidence_event_ids": json.loads(record["evidence_event_ids"])}

    @staticmethod
    def _latest_adjudication(connection: sqlite3.Connection, story_id: str, revision: int) -> dict | None:
        """Return the latest adjudication only while it remains in force.

        A withdrawal never promotes an older, already-superseded conclusion: the
        latest adjudication governs its story revision, and withdrawing it leaves
        that revision without a moderator conclusion until a new one is recorded.
        """
        row = connection.execute(
            "SELECT a.*, w.withdrawal_id AS withdrawal_id FROM adjudications a "
            "LEFT JOIN adjudication_withdrawals w ON w.adjudication_id=a.adjudication_id "
            "WHERE a.story_id=? AND a.story_revision=? "
            "ORDER BY a.sequence DESC LIMIT 1",
            (story_id, revision),
        ).fetchone()
        if row is None or row["withdrawal_id"] is not None:
            return None
        record = dict(row)
        record.pop("withdrawal_id")
        return record

    def latest_decisions_for_stories(self, identities: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
        result = {}
        for story_id, revision in identities:
            result[(story_id, revision)] = self.latest_decisions(story_id, revision)
        return result

    def analysis_snapshot(
        self, tradition: str | None
    ) -> tuple[list[dict], dict, dict, dict, dict]:
        with self.read_connection() as connection:
            connection.execute("BEGIN")
            if tradition is None:
                rows = connection.execute(
                    "SELECT document FROM stories ORDER BY tradition, story_id, revision"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT document FROM stories WHERE tradition=? ORDER BY story_id, revision", (tradition,)
                ).fetchall()
            stories = [json.loads(row["document"]) for row in rows]
            identities = [(item["story_id"], item["revision"]) for item in stories]
            decisions = {}
            adjudications = {}
            withdrawn = {}
            for story_id, revision in identities:
                latest = connection.execute(
                    "SELECT d.* FROM decisions d JOIN ("
                    "SELECT annotator, MAX(sequence) AS latest FROM decisions "
                    "WHERE story_id=? AND story_revision=? GROUP BY annotator"
                    ") newest ON d.annotator=newest.annotator AND d.sequence=newest.latest "
                    "WHERE d.story_id=? AND d.story_revision=? ORDER BY d.annotator",
                    (story_id, revision, story_id, revision),
                ).fetchall()
                decisions[(story_id, revision)] = [
                    dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in latest
                ]
                adjudications[(story_id, revision)] = self._latest_adjudication(
                    connection, story_id, revision
                )
                if adjudications[(story_id, revision)] is not None:
                    adjudications[(story_id, revision)] = adjudications[(story_id, revision)] | {
                        "evidence_event_ids": json.loads(adjudications[(story_id, revision)]["evidence_event_ids"])
                    }
                latest_adjudication = connection.execute(
                    "SELECT a.*, w.withdrawal_id AS withdrawal_id, w.reason AS withdrawal_reason, "
                    "w.moderator AS withdrawal_moderator, w.recorded_at AS withdrawal_recorded_at "
                    "FROM adjudications a "
                    "LEFT JOIN adjudication_withdrawals w ON w.adjudication_id=a.adjudication_id "
                    "WHERE a.story_id=? AND a.story_revision=? ORDER BY a.sequence DESC LIMIT 1",
                    (story_id, revision),
                ).fetchone()
                withdrawn[(story_id, revision)] = (
                    dict(latest_adjudication) | {
                        "evidence_event_ids": json.loads(latest_adjudication["evidence_event_ids"])
                    }
                    if latest_adjudication and latest_adjudication["withdrawal_id"] is not None else None
                )
            origins = {identity: [] for identity in identities}
            if identities:
                rows = connection.execute(
                    "SELECT batch_id, story_id, revision FROM batch_stories ORDER BY batch_id"
                ).fetchall()
                for row in rows:
                    identity = (row["story_id"], row["revision"])
                    if identity in origins:
                        origins[identity].append(row["batch_id"])
        return stories, decisions, origins, adjudications, withdrawn

    def catalog_manifest(self) -> tuple[list[dict], list[dict]]:
        with self.read_connection() as connection:
            batches = connection.execute(
                "SELECT b.batch_id, b.digest, b.story_count, COUNT(bs.story_id) AS linked_story_count "
                "FROM import_batches b LEFT JOIN batch_stories bs ON bs.batch_id=b.batch_id "
                "GROUP BY b.batch_id ORDER BY b.batch_id"
            ).fetchall()
            stories = connection.execute(
                "SELECT document FROM stories ORDER BY story_id, revision"
            ).fetchall()
        return [dict(row) for row in batches], [json.loads(row["document"]) for row in stories]

    def catalog_audit_snapshot(self) -> dict:
        with self.read_connection() as connection:
            return {
                "stories": [dict(row) for row in connection.execute("SELECT * FROM stories ORDER BY story_id, revision")],
                "batches": [dict(row) for row in connection.execute("SELECT * FROM import_batches ORDER BY batch_id")],
                "batch_stories": [dict(row) for row in connection.execute(
                    "SELECT * FROM batch_stories ORDER BY batch_id, story_id, revision")],
                "decisions": [dict(row) for row in connection.execute("SELECT * FROM decisions ORDER BY decision_id")],
                "adjudications": [dict(row) for row in connection.execute(
                    "SELECT * FROM adjudications ORDER BY adjudication_id")],
                "adjudication_withdrawals": [dict(row) for row in connection.execute(
                    "SELECT * FROM adjudication_withdrawals ORDER BY withdrawal_id")],
                "reports": [dict(row) for row in connection.execute("SELECT * FROM reports ORDER BY report_id")],
            }

    def report_document(self, report_id: str) -> dict | None:
        with self.read_connection() as connection:
            row = connection.execute("SELECT document FROM reports WHERE report_id=?", (report_id,)).fetchone()
        return json.loads(row["document"]) if row else None

    def integrity_issues(self) -> list[str]:
        with self.read_connection() as connection:
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        issues = [f"foreign key violation: {tuple(row)}" for row in foreign_keys]
        if quick_check != "ok":
            issues.append(f"SQLite quick_check: {quick_check}")
        return issues
