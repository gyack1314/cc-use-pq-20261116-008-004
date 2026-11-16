from __future__ import annotations

import unittest

from talent_harvest.cli import main
from talent_harvest.export import Delivery
from talent_harvest.inventory import Inventory
from talent_harvest.lineage import Lineage
from talent_harvest.promises import PromiseReview
from tests.support import Workspace, manifest, record


def project(record_id="proj-1", organization="Acme Corp", quantity=20,
            reference="jobs:post-7", title="Joint AI Program", **overrides):
    return record(record_id=record_id, organization=organization, title=title,
                  quantity=quantity, reference=reference,
                  description="Joint training project", **overrides)


def position(record_id="post-7", organization="Acme Corp", quantity=20, **overrides):
    return record(record_id=record_id, organization=organization, title="AI Engineer",
                  quantity=quantity, reference="https://example.test/post-7", **overrides)


class PromiseTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()
        self.work.register(key="jobs")
        self.work.register(key="partners", kind="partnership")

    def tearDown(self):
        self.work.close()

    def review(self):
        return PromiseReview(self.work.ledger).review()

    def only_promise(self):
        promises = self.review()["promises"]
        self.assertEqual(len(promises), 1)
        return promises[0]

    def test_project_before_position_stays_pending_then_verifies(self):
        self.work.ingest(key="partners", pages=[[project()]])
        view = Delivery(self.work.ledger).current(include_quarantine=True)
        self.assertEqual(view["counts"]["accepted"], 1)
        self.assertEqual(view["counts"]["quarantined"], 0)
        promises = self.review()
        self.assertEqual(promises["counts"], {"verified": 0, "conflicted": 0, "pending": 1})
        self.assertEqual(promises["slots"], {"verified": 0, "conflicted": 0, "pending": 20, "total": 20})
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("pending", "target_missing"))
        self.assertIsNone(entry["target"])
        self.assertEqual(entry["promised"], 20)
        self.work.ingest(key="jobs", pages=[[position()]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("verified", ""))
        self.assertEqual(entry["target"]["version"], 1)
        self.assertEqual(self.review()["slots"]["verified"], 20)

    def test_malformed_citation_is_quarantined_not_tracked(self):
        bad = ["https://example.test/x", "jobs:", "JOBS:post-7",
               "jobs:post 7", ":post-7", "jobs:post-7:extra"]
        rows = [project(record_id=f"proj-{index}", reference=reference)
                for index, reference in enumerate(bad, 1)]
        rows.append(project(record_id="proj-missing", reference=""))
        _, result = self.work.ingest(key="partners", pages=[rows])
        self.assertEqual(result["state"], "complete")
        view = Delivery(self.work.ledger).current(include_quarantine=True)
        self.assertEqual(view["counts"]["accepted"], 0)
        self.assertEqual(view["counts"]["quarantined"], 7)
        reasons = Inventory(self.work.ledger).quality_report()["by_reason"]
        self.assertEqual(reasons["project_reference_invalid"], 6)
        self.assertEqual(reasons["project_reference_missing"], 1)
        self.assertEqual(self.review()["promises"], [])

    def test_quarantined_target_keeps_promise_pending_until_fixed(self):
        self.work.ingest(key="jobs", pages=[[position(quantity=0)]])
        self.work.ingest(key="partners", pages=[[project()]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("pending", "target_quarantined"))
        self.assertIn("zero_quantity", entry["target"]["reasons"])
        self.assertEqual(self.review()["slots"]["pending"], 20)
        self.work.ingest(key="jobs", batch="batch-2", pages=[[position(quantity=20)]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("verified", ""))
        self.assertEqual(entry["target"]["version"], 2)

    def test_quantity_revision_makes_promise_conflicted(self):
        self.work.ingest(key="jobs", pages=[[position(quantity=25)]])
        self.work.ingest(key="partners", pages=[[project(quantity=20)]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("conflicted", "quantity_mismatch"))
        self.assertEqual(entry["target"]["quantity"], 25)
        self.work.ingest(key="partners", batch="batch-2", pages=[[project(quantity=25)]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("verified", ""))
        self.assertEqual(entry["version"], 2)
        history = Lineage(self.work.ledger).record("partners", "proj-1")
        self.assertEqual([item["record"]["quantity"] for item in history["versions"]], [20, 25])
        self.work.ingest(key="jobs", batch="batch-2", pages=[[position(quantity=26)]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("conflicted", "quantity_mismatch"))

    def test_dispute_and_withdrawal_drive_promise_status(self):
        self.work.register(key="jobs-alt")
        self.work.ingest(key="jobs", pages=[[position()]])
        self.work.ingest(key="partners", pages=[[project()]])
        self.assertEqual(self.only_promise()["status"], "verified")
        self.work.ingest(key="jobs-alt", pages=[[position(record_id="post-7b", quantity=23)]])
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["conflicts"], 1)
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("conflicted", "target_disputed"))
        self.assertTrue(entry["target"]["in_conflict"])
        self.work.harvester.withdraw("jobs-alt", "retracted")
        self.assertEqual(self.only_promise()["status"], "verified")

    def test_position_source_withdrawal_returns_promise_to_pending(self):
        self.work.ingest(key="jobs", pages=[[position()]])
        self.work.ingest(key="partners", pages=[[project()]])
        self.assertEqual(self.only_promise()["status"], "verified")
        self.work.harvester.withdraw("jobs", "source retired")
        current = Delivery(self.work.ledger).current()
        self.assertEqual(current["counts"]["accepted"], 1)
        self.assertEqual(current["accepted"][0]["record"]["kind"], "partnership")
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("pending", "target_withdrawn"))
        self.assertTrue(entry["target"]["withdrawn"])
        self.assertEqual(self.review()["slots"]["pending"], 20)

    def test_same_position_code_across_enterprises_does_not_cross_wire(self):
        self.work.register(key="jobs-b")
        self.work.ingest(key="jobs", pages=[[position()]])
        self.work.ingest(key="jobs-b", pages=[[position(organization="Beta Corp")]])
        rows = [project(record_id="proj-a", reference="jobs:post-7"),
                project(record_id="proj-b", organization="Beta Corp", reference="jobs-b:post-7"),
                project(record_id="proj-x", organization="Beta Corp", reference="jobs:post-7")]
        self.work.ingest(key="partners", pages=[rows])
        promises = {entry["record_id"]: entry for entry in self.review()["promises"]}
        self.assertEqual(promises["proj-a"]["status"], "verified")
        self.assertEqual(promises["proj-b"]["status"], "verified")
        self.assertEqual((promises["proj-x"]["status"], promises["proj-x"]["detail"]),
                         ("conflicted", "organization_mismatch"))

    def test_failed_page_retry_does_not_double_count_promises(self):
        path = manifest(self.work.path, key="partners",
                        pages=[[project(record_id="proj-1")],
                               [project(record_id="proj-2", reference="jobs:post-8")]])
        page_2 = path.parent / "page-2.json"
        original = page_2.read_bytes()
        page_2.unlink()
        failed = self.work.harvester.ingest(path)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual([page["state"] for page in failed["pages"]], ["complete", "failed"])
        self.assertEqual(len(self.review()["promises"]), 1)
        page_2.write_bytes(original)
        recovered = self.work.harvester.resume(path)
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(self.work.ledger.page("partners", "batch-1", 2)["attempts"], 2)
        promises = self.review()
        self.assertEqual(len(promises["promises"]), 2)
        self.assertEqual(promises["slots"]["pending"], 40)
        again = self.work.harvester.ingest(path)
        self.assertTrue(all(page["unchanged"] for page in again["pages"]))
        self.assertEqual(len(self.review()["promises"]), 2)
        observations = [row for row in self.work.ledger.observations() if row["source"] == "partners"]
        self.assertEqual(len(observations), 2)

    def test_citation_to_non_position_is_conflicted(self):
        self.work.register(key="programs", kind="program")
        self.work.ingest(key="programs", pages=[[record(record_id="prog-1", title="AI Masters")]])
        self.work.ingest(key="partners", pages=[[project(reference="programs:prog-1")]])
        entry = self.only_promise()
        self.assertEqual((entry["status"], entry["detail"]), ("conflicted", "target_not_position"))

    def test_cli_promises_command(self):
        self.work.ingest(key="partners", pages=[[project()]])
        code = main(["--db", str(self.work.path / "state.sqlite3"), "promises"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
