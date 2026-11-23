from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from tools.gap_benchmark import parent_metrics, parent_retest, parent_splits
from tools.gap_benchmark.data import save_new


def _claim(number, text, cites, evidence=()):
    return {
        "id": number,
        "claim": text,
        "cited_doc_ids": cites,
        "evidence": {str(doc): [{"label": "SUPPORT", "sentences": [0]}] for doc in evidence},
    }


def test_split_groups_transitive_overlap_and_old_exposure():
    docs = [
        {"doc_id": "1", "title": "Observed relation in experimental trial", "abstract": "A"},
        {"doc_id": "2", "title": "Observed relation in experimental trial", "abstract": "B"},
        {"doc_id": "3", "title": "A separate experimental publication", "abstract": "C"},
        {"doc_id": "4", "title": "A third experimental publication", "abstract": "D"},
    ]
    exposed = [_claim(1, "Old long claim about some evidence", [1])]
    fresh = [
        _claim(2, "Completely different question", [2], [3]),
        _claim(
            3, "One much longer claim involving an experimental setting and related measure", [3]
        ),
        _claim(
            4, "One much longer claim involving an experimental setting and related measures", [4]
        ),
        _claim(5, "A genuinely independent subject", []),
    ]
    rows = parent_splits.family_partitions(fresh, exposed, docs)
    assert rows == parent_splits.family_partitions(fresh[::-1], exposed, docs[::-1])
    assert {row["family"] for row in rows[:4]} == {"sf-family-1"}
    assert {row["split"] for row in rows[:4]} == {"train"}
    assert rows[4]["family"] != rows[0]["family"]


def test_gold_scoring_requires_first_id_and_supplied_directional_quote():
    import json

    text = "The experimental intervention improved performance compared with its control group."
    gold = {
        "1": [{"label": "SUPPORT", "sentences": [0]}],
        "2": [{"label": "CONTRADICT", "sentences": [0]}],
    }
    sources = {key: {"abstract": text, "abstract_sentences": [text]} for key in ("1", "2", "3")}
    papers = [
        {"doc_id": "1", "stance": "SUPPORT", "quote": "group", "reason": "Too short"},
        {"doc_id": "1", "stance": "SUPPORT", "quote": text, "reason": "Duplicate can't repair it"},
        {"doc_id": "2", "stance": "CONTRADICT", "quote": text, "reason": "Not supplied"},
        {"doc_id": "3", "stance": "SUPPORT", "quote": text, "reason": "Unknown to the gold"},
    ]
    record = {
        "completed": True,
        "model_valid": True,
        "secret_leak": False,
        "response_text": json.dumps({"papers": papers, "conclusion": "Partial"}),
    }
    result = parent_metrics.score(record, gold, sources, {"1", "3"})
    assert result["directional_coverage"] == result["full_rationale_coverage"] == 0
    assert result["supplied_recall"] == result["recall_at_10"] == 0.5
    assert result["invalid_quote"] == 2
    assert result["unsupplied_id"] == result["duplicate_id"] == result["unjudged_direction"] == 1
    assert result["opposite"] == 0


def test_variant_selection_refuses_tradeoff_in_known_or_unreviewed_errors():
    assessment = {
        "paired": {
            "separate": {"delta": 0.12, "wins": 3, "guardrail_blockers": ["opposite"]},
            "scoped": {"delta": 0.08, "wins": 3, "guardrail_blockers": []},
        }
    }
    assert parent_metrics.select_variant(assessment) == "scoped"
    assert parent_metrics.test_passed(assessment, "scoped")
    assessment["paired"]["scoped"]["guardrail_blockers"] = [
        "unjudged_direction_increase_requires_independent_review"
    ]
    assert parent_metrics.select_variant(assessment) is None


def test_high_effort_changes_only_the_parent_request_payload():
    options = {
        "model": "gpt-6-luna",
        "api_key": SecretStr("test-only"),
        "use_responses_api": True,
        "store": False,
    }
    parent = ChatOpenAI(**options)
    other = ChatOpenAI(**options)
    gateway = SimpleNamespace(agent_model=lambda: parent)
    messages = [HumanMessage("Read the supplied public evidence and return JSON.")]
    before = parent._get_request_payload(messages)
    parent_retest._set_parent_effort(gateway, "high")
    after = parent._get_request_payload(messages)
    assert after == {**before, "reasoning": {"effort": "high"}}
    assert other._get_request_payload(messages) == before
    assert after["store"] is False
    for arm in ("legacy", "high"):
        assert parent_retest._observation_mode(arm) == "legacy"


def test_decision_arm_uses_disjoint_validation_families_and_original_holdout():
    pools = {"support": 8, "contradict": 6, "evidence_gap": 6, "multi_document": 1}
    members = [
        {
            "case_id": f"{label}-{i}",
            "family": f"f-{label}-{i}",
            "split": "validation",
            "stratum": label,
        }
        for label, count in pools.items()
        for i in range(count)
    ]
    members.append({**members[0], "case_id": "sibling", "family": "f-contradict-0"})
    manifest = {
        "cohorts": {
            "train": ["t"],
            "validation": ["multi_document-0"],
            "test": ["h"],
            "end_to_end": ["e"],
        },
        "members": members
        + [
            {"case_id": k, "family": k, "split": split}
            for k, split in (("t", "train"), ("h", "test"), ("e", "test"))
        ],
    }
    cohorts = parent_retest._cohorts(manifest, "decision")
    families = {row["case_id"]: row["family"] for row in manifest["members"]}
    original = manifest["cohorts"]
    assert len({families[key] for key in cohorts["validation"]}) == 16
    for name in ("train", "test", "end_to_end"):
        assert cohorts[name] == original[name]
    assert {families[key] for key in cohorts["validation"]}.isdisjoint(
        families[key] for cases in original.values() for key in cases
    )


def test_decision_arm_changes_only_system_guidance(tmp_path):
    task = {
        "retriever": "hybrid",
        "dense_threshold": 0.8,
        "request": {"input_type": "abstract", "abstract": "Public claim"},
        "baseline_query": "Please answer about the public claim.",
    }
    observation = {"candidate_diagnostics": []}
    legacy = parent_retest._replay_request(task, observation, tmp_path, "legacy")
    rubric = parent_retest._replay_request(task, observation, tmp_path, "rubric")
    from citation_tool.agent import EVIDENCE_DECISION_GUIDANCE

    assert rubric[1].content == legacy[1].content
    assert (
        rubric[0].content.replace("\n\n" + EVIDENCE_DECISION_GUIDANCE, "", 1) == legacy[0].content
    )


def test_secondlook_never_overwrites_a_direction_or_accepts_outside_evidence():
    from tools.gap_benchmark.metrics import FinalAnswer, PaperAnswer
    from tools.luna_secondlook_replay import merge_evidence, review_pool

    quote = "The measured endpoint improved in treated participants."
    certain = PaperAnswer(doc_id="1", stance="SUPPORT", quote=quote, reason="direct evidence")
    uncertain = PaperAnswer(doc_id="2", stance="INSUFFICIENT", quote=quote, reason="uncertain")
    baseline = FinalAnswer(papers=[certain, uncertain], conclusion="Baseline conclusion")
    docs = review_pool(
        {
            "candidate_diagnostics": [
                {"work_id": f"work:scifact:{i}", "title": f"Title {i}", "evidence_excerpt": quote}
                for i in (1, 2)
            ]
        },
        baseline,
    )
    better = uncertain.model_copy(update={"stance": "SUPPORT"})
    proposed = FinalAnswer(papers=[better], conclusion="Reconsidered")
    assert list(docs) == ["2"]
    assert merge_evidence(baseline, proposed, docs).papers == [certain, better]
    for invalid in [certain, better.model_copy(update={"quote": "Not in the abstract."})]:
        with pytest.raises(ValueError, match="invalid evidence proposal"):
            merge_evidence(baseline, FinalAnswer(papers=[invalid], conclusion="Invalid"), docs)


def test_failed_validation_does_not_open_holdout(tmp_path):
    digest = "frozen-plan"
    save_new(
        tmp_path / "validation" / "assessment.json",
        {"cohort": "validation", "selected_arm": None, "plan_sha256": digest},
    )
    with pytest.raises(ValueError, match="quality gate stayed closed"):
        parent_retest._cohort_arms(tmp_path, "test", digest, "reasoning")
    assert not (tmp_path / "test").exists()
