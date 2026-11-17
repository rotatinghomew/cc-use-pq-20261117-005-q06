from __future__ import annotations

from datetime import datetime, timezone

from story_patterns.catalog.validate import canonical_json, digest_json, _text
from story_patterns.errors import ConflictError, NotFoundError, ValidationError


def normalize_adjudication(raw: dict, story: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("adjudication must be an object")
    record_id = _text(raw.get("adjudication_id"), "adjudication_id", 120)
    moderator = _text(raw.get("moderator"), "moderator", 120)
    sequence = raw.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValidationError("sequence must be a positive integer")
    pattern = raw.get("pattern")
    outcome = raw.get("youngest_outcome")
    if pattern not in {"parallel", "harm"}:
        raise ValidationError("an adjudicated pattern must be parallel or harm")
    if outcome not in {"wins", "loses", "mixed", "unknown"}:
        raise ValidationError("youngest_outcome is not supported")
    evidence = raw.get("evidence_event_ids")
    known = {event["event_id"] for event in story["events"]}
    if not isinstance(evidence, list) or not evidence:
        raise ValidationError("adjudication must cite at least one event")
    if any(not isinstance(item, str) or item not in known for item in evidence):
        raise ValidationError("adjudication cites an event outside this story revision")
    if len(evidence) != len(set(evidence)):
        raise ValidationError("adjudication evidence cannot repeat")
    rationale = _text(raw.get("rationale"), "rationale", 1500)
    return {
        "adjudication_id": record_id,
        "story_id": story["story_id"], "story_revision": story["revision"],
        "moderator": moderator, "sequence": sequence, "pattern": pattern,
        "youngest_outcome": outcome, "evidence_event_ids": sorted(evidence),
        "rationale": rationale,
    }


def _timestamp(value) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError("recorded_at must be an ISO-8601 timestamp") from error
        if moment.tzinfo is None:
            raise ValidationError("recorded_at must include a UTC offset")
        moment = moment.astimezone(timezone.utc)
    else:
        raise ValidationError("recorded_at must be an ISO-8601 timestamp")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_withdrawal(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("withdrawal must be an object")
    return {
        "withdrawal_id": _text(raw.get("withdrawal_id"), "withdrawal_id", 120),
        "adjudication_id": _text(raw.get("adjudication_id"), "adjudication_id", 120),
        "moderator": _text(raw.get("moderator"), "moderator", 120),
        "reason": _text(raw.get("reason"), "reason", 1500),
        "recorded_at": _timestamp(raw.get("recorded_at")),
    }


class AdjudicationService:
    def __init__(self, store):
        self.store = store

    def record(self, raw: dict) -> dict:
        story_id = raw.get("story_id") if isinstance(raw, dict) else None
        revision = raw.get("story_revision") if isinstance(raw, dict) else None
        story = self.store.story_document(story_id, revision)
        if story is None:
            raise NotFoundError(f"story revision {story_id!r}/{revision!r} was not found")
        record = normalize_adjudication(raw, story)
        digest = digest_json(record)
        with self.store.transaction() as connection:
            previous = connection.execute(
                "SELECT digest FROM adjudications WHERE adjudication_id=?", (record["adjudication_id"],)
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise ConflictError("adjudication identifier already refers to different content")
                return {"adjudication_id": record["adjudication_id"], "status": "already_recorded"}
            occupied = connection.execute(
                "SELECT adjudication_id FROM adjudications WHERE story_id=? AND story_revision=? AND sequence=?",
                (story_id, revision, record["sequence"]),
            ).fetchone()
            if occupied:
                raise ConflictError("adjudication sequence is already occupied for this story revision")
            connection.execute(
                "INSERT INTO adjudications(adjudication_id, story_id, story_revision, moderator, sequence, pattern, "
                "youngest_outcome, evidence_event_ids, rationale, digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["adjudication_id"], story_id, revision, record["moderator"], record["sequence"],
                 record["pattern"], record["youngest_outcome"], canonical_json(record["evidence_event_ids"]),
                 record["rationale"], digest),
            )
        return {"adjudication_id": record["adjudication_id"], "status": "recorded"}

    def withdraw(self, raw: dict) -> dict:
        """Retire one adjudication without deleting it or promoting an older one."""
        record = normalize_withdrawal(raw)
        digest = digest_json(record)
        with self.store.transaction() as connection:
            previous = connection.execute(
                "SELECT adjudication_id, moderator, reason, recorded_at FROM adjudication_withdrawals "
                "WHERE withdrawal_id=?", (record["withdrawal_id"],)
            ).fetchone()
            if previous:
                same_request = (
                    previous["adjudication_id"] == record["adjudication_id"]
                    and previous["moderator"] == record["moderator"]
                    and previous["reason"] == record["reason"]
                    and (raw.get("recorded_at") is None or previous["recorded_at"] == record["recorded_at"])
                )
                if not same_request:
                    raise ConflictError("withdrawal identifier already refers to different content")
                return {"withdrawal_id": record["withdrawal_id"], "status": "already_recorded"}
            adjudication = connection.execute(
                "SELECT story_id, story_revision, sequence FROM adjudications WHERE adjudication_id=?",
                (record["adjudication_id"],),
            ).fetchone()
            if adjudication is None:
                raise NotFoundError(f"adjudication {record['adjudication_id']!r} was not found")
            existing = connection.execute(
                "SELECT withdrawal_id FROM adjudication_withdrawals WHERE adjudication_id=?",
                (record["adjudication_id"],),
            ).fetchone()
            if existing:
                raise ConflictError(
                    f"adjudication {record['adjudication_id']} was already withdrawn by {existing['withdrawal_id']}"
                )
            latest = connection.execute(
                "SELECT adjudication_id FROM adjudications WHERE story_id=? AND story_revision=? "
                "ORDER BY sequence DESC LIMIT 1",
                (adjudication["story_id"], adjudication["story_revision"]),
            ).fetchone()
            if latest["adjudication_id"] != record["adjudication_id"]:
                raise ConflictError(
                    "only the most recent adjudication for a story revision can be withdrawn; "
                    "record a corrective adjudication instead"
                )
            connection.execute(
                "INSERT INTO adjudication_withdrawals(withdrawal_id, adjudication_id, moderator, reason, "
                "recorded_at, digest) VALUES(?,?,?,?,?,?)",
                (record["withdrawal_id"], record["adjudication_id"], record["moderator"],
                 record["reason"], record["recorded_at"], digest),
            )
        return {
            "withdrawal_id": record["withdrawal_id"],
            "adjudication_id": record["adjudication_id"],
            "status": "withdrawn",
        }
