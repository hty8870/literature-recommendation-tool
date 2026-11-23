from tools.scifact_rank_reason_retest import (
    direction_conflicts,
    observed_reasons,
    unjudged_direction,
)
from tools.scifact_workspace_retest import EvidenceAudit

from citation_tool.agent_observations import hide_rank_rationale


def test_receipt_audit_counts_reasons_without_changing_original_evidence(tmp_path):
    text = "The source uses the specified control."
    original = {
        "candidate_diagnostics": [
            {
                "work_id": "work:scifact:105",
                "evidence_excerpt": text,
                "llm_rank": 1,
                "reason": {"status": "generated", "text": "Model reason."},
            }
        ]
    }
    audit = EvidenceAudit(tmp_path, {"105": text})
    audit.observe("search_literature", original)
    audit.observe("search_literature", hide_rank_rationale(original))
    assert observed_reasons({"evidence_deliveries": audit.deliveries[:1]}) == (1, 1)
    assert observed_reasons({"evidence_deliveries": audit.deliveries[1:]}) == (0, 1)
    assert audit.deliveries[0]["spans"] == audit.deliveries[1]["spans"] == {"105": [(0, len(text))]}


def test_official_abstention_is_not_opposite_direction():
    def paper(doc, stance, grade, *, duplicate=False):
        return {
            "doc_id": doc,
            "stance": stance,
            "stance_correct": grade,
            "official_evidence": grade is not None,
            "duplicate": duplicate,
        }

    record = {
        "assessment": {
            "papers": [
                paper("1", "INSUFFICIENT", False),
                paper("2", "SUPPORT", False),
                paper("2", "SUPPORT", False, duplicate=True),
                paper("3", "INSUFFICIENT", None),
                paper("4", "CONTRADICT", None),
            ]
        }
    }
    assert direction_conflicts([record]) == 1
    assert unjudged_direction(record) == {("4", "CONTRADICT")}
