import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from story_patterns.cli import main
from story_patterns.errors import ConflictError
from tests.support import AppCase, batch, decision, fixture, story


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "demo.json"


class EndToEndResearchWorkflowTests(AppCase, unittest.TestCase):
    def test_import_review_queue_and_report_show_consistent_cross_module_state(self):
        records = [story("stone"), story("orchard", tradition="coast")]
        imported = self.app.import_batch(batch(records, "research-cohort"))
        self.assertEqual(imported["story_count"], 2)
        self.app.record_decision(decision("stone", annotator="reader-a"))
        queue = self.app.review_queue()
        self.assertEqual(queue["by_status"]["needs_more_reviews"], 1)
        self.assertEqual(queue["by_status"]["unreviewed"], 1)
        self.app.record_decision(decision("stone", annotator="reader-b"))
        report = self.app.create_report()
        self.assertEqual(report["pattern_counts"], {"parallel": 1})
        self.assertEqual(report["population"]["story_revisions"], 2)
        self.assertEqual(report["provenance"]["decision_ids"], ["stone-reader-a-1", "stone-reader-b-1"])

    def test_concurrent_style_revisions_do_not_leak_old_decisions_into_report(self):
        self.import_story(story(revision=1), "rev1")
        self.app.import_batch(batch([story(revision=2)], "rev2"))
        self.app.record_decision(decision(revision=1, annotator="reader-a"))
        self.app.record_decision(decision(revision=1, annotator="reader-b"))
        report = self.app.create_report()
        rows = {(row["story_id"], row["revision"]): row for row in report["story_results"]}
        self.assertEqual(rows[("tale-a", 1)]["pattern"], "parallel")
        self.assertEqual(rows[("tale-a", 2)]["pattern_status"], "insufficient")

    def test_failed_second_review_does_not_corrupt_existing_analysis(self):
        self.import_story()
        self.app.record_decision(decision(annotator="reader-a"))
        with self.assertRaises(ConflictError):
            self.app.record_decision(decision(annotator="reader-a", decision_id="other-id"))
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_count"], 1)

    def test_integrity_audit_and_new_application_reopen_persisted_data(self):
        self.import_story()
        self.add_votes()
        report = self.app.create_report()
        self.assertEqual(self.app.store.integrity_issues(), [])
        reopened = type(self.app)(self.database)
        self.assertEqual(reopened.stored_report(report["report_id"])["pattern_counts"], {"parallel": 1})
        self.assertEqual(reopened.review_queue()["by_status"]["ready"], 1)

    def test_durable_store_rejects_ephemeral_memory_database_explicitly(self):
        from story_patterns.storage import StoryStore

        with self.assertRaisesRegex(ValueError, "file-backed"):
            StoryStore(":memory:")

    def test_demo_fixture_is_local_synthetic_and_exercises_analytics(self):
        content = fixture(FIXTURE)
        self.assertTrue(all(story_data["source"]["kind"] == "local_synthetic"
                            for story_data in content["batch"]["stories"]))
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "demo.sqlite3")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["demo", "--database", database, "--fixture", str(FIXTURE)])
        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["report"]["pattern_counts"], {"harm": 1, "parallel": 1})
        self.assertEqual(output["report"]["population"]["story_revisions"], 2)

    def test_cli_reports_input_errors_without_success_shaped_json(self):
        with tempfile.TemporaryDirectory() as directory:
            bad_file = Path(directory) / "broken.json"
            bad_file.write_text("not json", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(["import", "--database", str(Path(directory) / "db.sqlite3"), "--file", str(bad_file)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("error", json.loads(stderr.getvalue()))

    def test_cli_withdraws_adjudication_and_reports_reopened_state(self):
        from story_patterns.application import StoryPatternApplication

        self.import_story()
        self.add_votes(values=[
            decision(annotator="reader-a", pattern="parallel", outcome="wins"),
            decision(annotator="reader-b", pattern="harm", outcome="loses"),
        ])
        adjudication = {
            "adjudication_id": "cli-resolution", "story_id": "tale-a", "story_revision": 1,
            "moderator": "editor-1", "sequence": 1, "pattern": "harm", "youngest_outcome": "wins",
            "evidence_event_ids": ["first"], "rationale": "Synthetic moderator reading.",
        }
        withdrawal = {
            "withdrawal_id": "cli-withdrawal", "adjudication_id": "cli-resolution",
            "moderator": "editor-2", "reason": "Cited event was not reliable.",
            "recorded_at": "2026-11-17T12:00:00Z",
        }
        self.app.record_adjudication(adjudication)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "withdrawal.json"
            path.write_text(json.dumps(withdrawal), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["withdraw-adjudication", "--database", self.database, "--file", str(path)])
            output = json.loads(stdout.getvalue())
            again = io.StringIO()
            with contextlib.redirect_stdout(again):
                main(["withdraw-adjudication", "--database", self.database, "--file", str(path)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["status"], "withdrawn")
        self.assertEqual(json.loads(again.getvalue())["status"], "already_recorded")
        reopened = StoryPatternApplication(self.database)
        self.assertEqual(reopened.review_queue()["by_status"]["reopened"], 1)
        self.assertEqual(reopened.create_report()["resolution_counts"]["withdrawn"], 1)


class SyntheticAdjudicationRevisionFlowTests(AppCase, unittest.TestCase):
    def test_review_withdrawal_correction_and_report_change_end_to_end(self):
        # 1. Review: two independent readers disagree, so the record is disputed.
        self.app.import_batch(batch([story()], "synthetic-cohort"))
        self.add_votes(values=[
            decision(annotator="reader-a", pattern="parallel", outcome="wins", evidence=["first", "third"]),
            decision(annotator="reader-b", pattern="harm", outcome="loses", evidence=["first"]),
        ])
        self.assertEqual(self.app.review_queue()["stories"][0]["status"], "disputed")

        # 2. Adjudication: an editor resolves it using a later-corrected event.
        original = {"adjudication_id": "synthetic-resolution-1", "story_id": "tale-a", "story_revision": 1,
                    "moderator": "editor-1", "sequence": 1, "pattern": "harm", "youngest_outcome": "loses",
                    "evidence_event_ids": ["first"], "rationale": "Synthetic close reading."}
        first_report = self.app.create_report()
        self.app.record_adjudication(original)
        resolved = self.app.create_report()
        self.assertEqual(self.app.review_queue()["by_status"]["adjudicated"], 1)
        self.assertEqual(resolved["final_pattern_counts"], {"harm": 1})
        self.assertEqual(resolved["resolution_counts"], {"reader_consensus": 0, "adjudicated": 1, "withdrawn": 0})

        # 3. Withdrawal: the cited event is later shown to be unreliable.
        retraction = {"withdrawal_id": "synthetic-withdrawal-1", "adjudication_id": "synthetic-resolution-1",
                      "moderator": "editor-2", "reason": "Synthetic event was contradicted by a later source.",
                      "recorded_at": "2026-11-17T10:00:00Z"}
        self.assertEqual(self.app.withdraw_adjudication(retraction)["status"], "withdrawn")
        reopened = self.app.create_report()
        self.assertEqual(self.app.review_queue()["by_status"]["reopened"], 1)
        self.assertEqual(reopened["resolution_counts"], {"reader_consensus": 0, "adjudicated": 0, "withdrawn": 1})
        self.assertEqual(reopened["final_pattern_counts"], {})
        self.assertEqual(reopened["final_outcome_counts"], {})
        self.assertEqual(reopened["provenance"]["withdrawn_adjudication_ids"], ["synthetic-resolution-1"])
        self.assertEqual(reopened["story_results"][0]["pattern_status"], "split")

        # 4. History keeps both the retracted conclusion and the retraction trail;
        #    the previously saved report still reproduces the pre-withdrawal snapshot.
        history = self.app.review_history("tale-a", 1)
        self.assertEqual([item["status"] for item in history["adjudications"]], ["withdrawn"])
        self.assertEqual(history["adjudications"][0]["pattern"], "harm")
        self.assertEqual(history["withdrawals"][0]["reason"], retraction["reason"])
        self.assertEqual(self.app.stored_report(resolved["report_id"])["final_pattern_counts"], {"harm": 1})
        self.assertEqual(self.app.stored_report(first_report["report_id"])["final_pattern_counts"], {})

        # 5. Correction: a new adjudication supersedes the withdrawn conclusion;
        #    only the current conclusion counts and the whole change survives a reopen.
        correction = {"adjudication_id": "synthetic-resolution-2", "story_id": "tale-a", "story_revision": 1,
                      "moderator": "editor-2", "sequence": 2, "pattern": "parallel", "youngest_outcome": "wins",
                      "evidence_event_ids": ["first", "third"], "rationale": "Reliable events only."}
        self.app.record_adjudication(correction)
        reopened_app = type(self.app)(self.database)
        final = reopened_app.create_report()
        self.assertEqual(reopened_app.review_queue()["by_status"]["adjudicated"], 1)
        self.assertEqual(final["resolution_counts"], {"reader_consensus": 0, "adjudicated": 1, "withdrawn": 0})
        self.assertEqual(final["final_pattern_counts"], {"parallel": 1})
        self.assertEqual(final["final_outcome_counts"], {"wins": 1})
        self.assertEqual(final["provenance"]["adjudication_ids"], ["synthetic-resolution-2"])
        self.assertEqual(final["provenance"]["withdrawn_adjudication_ids"], [])
        self.assertTrue(reopened_app.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
