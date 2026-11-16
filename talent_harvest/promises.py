from __future__ import annotations

from .contracts import parse_citation
from .export import Delivery
from .ledger import Ledger
from .normalize import search_key


class PromiseReview:
    """Reconciles partnership promises against current position evidence.

    Promise status is derived at read time from the latest observations, so it
    always follows current position evidence: late-arriving pages, revisions,
    cross-source disputes, and source withdrawals are reflected without any
    stored promise state that could go stale or be counted twice on retries.
    """

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def review(self) -> dict:
        latest: dict[tuple[str, str], dict] = {}
        for row in self.ledger.observations():
            latest[row["source"], row["record_id"]] = row
        disputed = {item["identity"] for item in Delivery(self.ledger).current()["conflicts"]}
        promises = []
        for key in sorted(latest):
            row = latest[key]
            record = row["record"]
            if record["kind"] != "partnership" or row["status"] != "accepted" or row["withdrawn_at"]:
                continue
            citation = parse_citation(record["reference"])
            if citation is None:
                continue  # intake quarantines invalid citations; defensive skip
            promises.append(self._resolve(row, citation, latest, disputed))
        counts = {"verified": 0, "conflicted": 0, "pending": 0}
        slots = {"verified": 0, "conflicted": 0, "pending": 0}
        for promise in promises:
            counts[promise["status"]] += 1
            slots[promise["status"]] += promise["promised"]
        return {"schema_version": 1, "promises": promises, "counts": counts,
                "slots": {**slots, "total": sum(slots.values())}}

    def _resolve(self, row: dict, citation: tuple[str, str],
                 latest: dict[tuple[str, str], dict], disputed: set[str]) -> dict:
        record = row["record"]
        cited_source, cited_record = citation
        base = {"source": row["source"], "record_id": row["record_id"],
                "version": row["version"], "identity": row["identity"],
                "organization": record["organization"], "title": record["title"],
                "region": record["region"], "promised": record["quantity"],
                "citation": {"source": cited_source, "record_id": cited_record,
                             "raw": record["reference"]},
                "captured_at": row["captured_at"], "observed_at": row["observed_at"]}
        target_row = latest.get((cited_source, cited_record))
        if target_row is None:
            return {**base, "status": "pending", "detail": "target_missing", "target": None}
        target = self._target_evidence(target_row, disputed)
        if target_row["withdrawn_at"]:
            return {**base, "status": "pending", "detail": "target_withdrawn", "target": target}
        if target_row["status"] != "accepted":
            return {**base, "status": "pending", "detail": "target_quarantined", "target": target}
        target_record = target_row["record"]
        if target_record["kind"] != "position":
            return {**base, "status": "conflicted", "detail": "target_not_position", "target": target}
        if search_key(target_record["organization"]) != search_key(record["organization"]):
            return {**base, "status": "conflicted", "detail": "organization_mismatch", "target": target}
        if target["in_conflict"]:
            return {**base, "status": "conflicted", "detail": "target_disputed", "target": target}
        if target_record["quantity"] != record["quantity"]:
            return {**base, "status": "conflicted", "detail": "quantity_mismatch", "target": target}
        return {**base, "status": "verified", "detail": "", "target": target}

    @staticmethod
    def _target_evidence(row: dict, disputed: set[str]) -> dict:
        record = row["record"]
        return {"source": row["source"], "record_id": row["record_id"],
                "version": row["version"], "kind": record["kind"],
                "organization": record["organization"], "quantity": record["quantity"],
                "status": row["status"], "reasons": row["reasons"],
                "withdrawn": bool(row["withdrawn_at"]),
                "in_conflict": row["identity"] in disputed}
