from tools.luna_gap_retest import first_loss_by_target


def test_stage_loss_distinguishes_recall_submission_rank_and_final_selection():
    task = {"expect": {"evidence": {str(index): [] for index in range(1, 7)}}}
    record = {
        "queries": [
            {
                "workers": [
                    {"source_available_work_ids": [f"work:scifact:{i}" for i in range(2, 7)]}
                ],
                "candidate_diagnostics": [
                    {"work_id": "work:scifact:3", "base_rank": 11, "llm_rank": 12},
                    {"work_id": "work:scifact:4", "base_rank": 2, "llm_rank": 11},
                    {"work_id": "work:scifact:5", "base_rank": 3, "llm_rank": 3},
                    {"work_id": "work:scifact:6", "base_rank": 1, "llm_rank": 1},
                ],
            }
        ],
        "assessment": {"papers": [{"doc_id": "6", "rank": 1, "duplicate": False}]},
    }
    rows = first_loss_by_target(record, task)
    assert [row["first_missing_stage"] for row in rows] == [
        "retrieval",
        "source_submission",
        "common_top10",
        "llm_top10",
        "final_answer",
        None,
    ]
    assert all("not a causal ablation" in row["note"] for row in rows)


def test_missing_gold_does_not_manufacture_zero_recall_targets():
    assert first_loss_by_target({}, {"expect": {"evidence": {}}}) == []
