from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class PaperAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    stance: Literal["SUPPORT", "CONTRADICT", "INSUFFICIENT"]
    quote: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("doc_id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> str:
        match = re.fullmatch(r"(?:(?:work:)?scifact:)?([0-9]+)", str(value))
        if match is None:
            raise ValueError("doc_id must be a SciFact numeric document identity")
        return match.group(1)


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    papers: list[PaperAnswer] = Field(max_length=10)
    conclusion: str = Field(min_length=1)


def parse_answer(text: str) -> tuple[FinalAnswer | None, list[dict]]:
    content = text.strip()
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4].strip()
    elif content.startswith("```\n") and content.endswith("\n```"):
        content = content[4:-4].strip()
    try:
        return FinalAnswer.model_validate_json(content), []
    except ValidationError as exc:
        return None, [
            {"path": list(item["loc"]), "type": item["type"]}
            for item in exc.errors(include_input=False, include_context=False)
        ]


def retrieval_metrics(ids: list[str], evidence: dict, cutoff: int) -> dict:
    ranked = list(dict.fromkeys(ids))[:cutoff]
    targets = set(evidence)
    retrieved = targets & set(ranked)
    ideal = sum(1 / math.log2(index + 2) for index in range(min(len(targets), cutoff)))
    dcg = sum(1 / math.log2(index + 2) for index, value in enumerate(ranked) if value in targets)
    return {
        "recall": len(retrieved) / len(targets) if targets else None,
        "ndcg": dcg / ideal if ideal else None,
        "hit": bool(retrieved) if targets else None,
        "all_found": retrieved == targets if targets else None,
        "unjudged_fraction": sum(value not in targets for value in ranked) / len(ranked)
        if ranked
        else None,
        "ranked_ids": ranked,
    }


def score_record(record: dict, task: dict, corpus: dict) -> dict:
    by_id = {row["doc_id"]: row for row in corpus["records"]}
    evidence = task["expect"]["evidence"]
    answer, errors = parse_answer(record.get("response_text") or "")
    papers = answer.papers if answer is not None else []
    rows = []
    seen = set()
    for index, paper in enumerate(papers, 1):
        source = by_id.get(paper.doc_id)
        rationales = evidence.get(paper.doc_id, [])
        labels = {item["label"] for item in rationales}
        exact = bool(source and paper.quote in source["abstract"])
        sets = (
            [
                {
                    "sentence_ids": rationale["sentences"],
                    "all_sentences_in_quote": all(
                        source["abstract_sentences"][sentence] in paper.quote
                        for sentence in rationale["sentences"]
                    ),
                    "any_sentence_overlap": any(
                        source["abstract_sentences"][sentence] in paper.quote
                        or paper.quote in source["abstract_sentences"][sentence]
                        for sentence in rationale["sentences"]
                    ),
                }
                for rationale in rationales
            ]
            if source and exact
            else []
        )
        duplicate = paper.doc_id in seen
        seen.add(paper.doc_id)
        rows.append(
            {
                **paper.model_dump(),
                "rank": index,
                "known_id": source is not None,
                "duplicate": duplicate,
                "exact_quote": exact,
                "official_evidence": bool(rationales),
                "official_labels": sorted(labels),
                "stance_correct": paper.stance in labels if labels else None,
                "rationale_sets": sets,
                "official_rationale_complete": any(row["all_sentences_in_quote"] for row in sets),
                "official_rationale_overlap": any(row["any_sentence_overlap"] for row in sets),
            }
        )
    ranked = [row["doc_id"] for row in rows if not row["duplicate"]]
    correct = {
        row["doc_id"]
        for row in rows
        if row["official_evidence"] and row["stance_correct"] and row["exact_quote"]
    }
    complete_rationale = {row["doc_id"] for row in rows if row["official_rationale_complete"]}
    quality_valid = bool(
        record.get("completed")
        and record.get("model_valid")
        and not record.get("secret_leak")
        and record.get("session_isolation_valid", True)
        and record.get("project_skill_isolation_valid", True)
    )
    stages = {}
    if record["system"] == "product":
        stages = product_stages(record, evidence)
    return {
        **record,
        "slice": task["slice"],
        "family": task["family"],
        "assessment": {
            "valid_completion": quality_valid,
            "format_valid": answer is not None,
            "format_errors": errors,
            "papers": rows,
            "conclusion": answer.conclusion if answer else None,
            "retrieval_at_5": retrieval_metrics(ranked if quality_valid else [], evidence, 5),
            "retrieval_at_10": retrieval_metrics(ranked if quality_valid else [], evidence, 10),
            "partial_output_recall_at_10": retrieval_metrics(ranked, evidence, 10)["recall"],
            "correct_direction_evidence_coverage": len(correct) / len(evidence)
            if evidence and quality_valid
            else 0.0
            if evidence
            else None,
            "complete_rationale_coverage": len(complete_rationale) / len(evidence)
            if evidence and quality_valid
            else 0.0
            if evidence
            else None,
            "wrong_official_stances": sum(
                row["stance_correct"] is False and not row["duplicate"] for row in rows
            ),
            "invalid_quotes": sum(not row["exact_quote"] for row in rows),
            "unknown_ids": sum(not row["known_id"] for row in rows),
            "duplicate_ids": sum(row["duplicate"] for row in rows),
            "empty_answer": not rows if answer else None,
            "unjudged_requires_review": [
                row["doc_id"] for row in rows if not row["official_evidence"]
            ],
            "product_stages": stages,
        },
    }


def _doc_ids(values: list[str]) -> list[str]:
    return [value.removeprefix("work:scifact:") for value in values]


def product_stages(record: dict, evidence: dict) -> dict:
    available, submitted = set(), set()
    best_base, best_llm = 0.0, 0.0
    for query in record.get("queries", []):
        for worker in query.get("workers", []):
            available.update(_doc_ids(worker.get("source_available_work_ids", [])))
        candidates = query.get("candidate_diagnostics", [])
        submitted.update(_doc_ids([row["work_id"] for row in candidates]))
        for field, marker in (("base_rank", "base"), ("llm_rank", "llm")):
            eligible = sorted(
                [row for row in candidates if row.get(field) is not None],
                key=lambda row: (row[field], row["work_id"]),
            )
            recall = retrieval_metrics(
                _doc_ids([row["work_id"] for row in eligible]), evidence, 10
            )["recall"]
            if marker == "base":
                best_base = max(best_base, recall or 0.0)
            else:
                best_llm = max(best_llm, recall or 0.0)
    targets = set(evidence)
    return {
        "available_recall_union": len(available & targets) / len(targets) if targets else None,
        "submitted_recall_union": len(submitted & targets) / len(targets) if targets else None,
        "best_single_query_base_recall_at_10": best_base if targets else None,
        "best_single_query_llm_recall_at_10": best_llm if targets else None,
        "note": "union diagnostic is not comparable to a single top-10 final answer",
    }


def mean(values) -> float | None:
    available = [value for value in values if value is not None]
    return statistics.fmean(available) if available else None


def aggregate(records: list[dict]) -> dict:
    return {
        "runs": len(records),
        "completed": sum(row["assessment"]["valid_completion"] for row in records),
        "format_valid": sum(row["assessment"]["format_valid"] for row in records),
        "positive_cases": sum(
            row["assessment"]["correct_direction_evidence_coverage"] is not None for row in records
        ),
        "final_recall_at_5": mean(row["assessment"]["retrieval_at_5"]["recall"] for row in records),
        "final_recall_at_10": mean(
            row["assessment"]["retrieval_at_10"]["recall"] for row in records
        ),
        "final_ndcg_at_5": mean(row["assessment"]["retrieval_at_5"]["ndcg"] for row in records),
        "correct_direction_evidence_coverage": mean(
            row["assessment"]["correct_direction_evidence_coverage"] for row in records
        ),
        "complete_rationale_coverage": mean(
            row["assessment"]["complete_rationale_coverage"] for row in records
        ),
        **{
            name: sum(row["assessment"][name] for row in records)
            for name in ("wrong_official_stances", "invalid_quotes", "unknown_ids", "duplicate_ids")
        },
        "unjudged_papers": sum(
            len(row["assessment"]["unjudged_requires_review"]) for row in records
        ),
        "median_wall_time_s_resource_only": statistics.median(
            row["wall_time_ms"] / 1000 for row in records
        )
        if records
        else None,
    }


def paired_summary(records: list[dict]) -> dict:
    grouped = defaultdict(dict)
    for record in records:
        grouped[record["run_case_id"]][record["system"]] = record
    pairs = []
    clusters = defaultdict(list)
    for case_id, systems in grouped.items():
        if set(systems) != {"product", "kimi_code"}:
            raise ValueError("paired evaluation requires both systems for every case")
        first = systems["product"]
        product = first["assessment"]["correct_direction_evidence_coverage"]
        baseline = systems["kimi_code"]["assessment"]["correct_direction_evidence_coverage"]
        difference = product - baseline if product is not None and baseline is not None else None
        pairs.append({"case_id": case_id, "family": first["family"], "difference": difference})
        if difference is not None:
            clusters[first["family"]].append(difference)
    family_means = [statistics.fmean(values) for values in clusters.values()]
    rng = np.random.default_rng(20261117)
    bootstraps = (
        sorted(map(float, rng.choice(family_means, size=(5000, len(family_means))).mean(axis=1)))
        if family_means
        else []
    )
    systems = {
        name: aggregate([row for row in records if row["system"] == name])
        for name in ("product", "kimi_code")
    }
    mean_difference = mean(row["difference"] for row in pairs)
    win_families = sum(value > 0 for value in family_means)
    observed_advantage = bool(
        mean_difference is not None
        and mean_difference >= 0.10
        and win_families >= 3
        and systems["product"]["wrong_official_stances"]
        <= systems["kimi_code"]["wrong_official_stances"]
        and systems["product"]["invalid_quotes"] <= systems["kimi_code"]["invalid_quotes"]
    )
    return {
        "systems": systems,
        "slices": {
            slice_name: {
                name: aggregate(
                    [row for row in records if row["system"] == name and row["slice"] == slice_name]
                )
                for name in systems
            }
            for slice_name in dict.fromkeys(row["slice"] for row in records)
        },
        "paired": {
            "case_mean_difference": mean_difference,
            "family_mean_difference": mean(family_means),
            "positive_families": len(family_means),
            "product_win_families": win_families,
            "kimi_win_families": sum(value < 0 for value in family_means),
            "tie_families": sum(value == 0 for value in family_means),
            "descriptive_cluster_bootstrap_95_interval": [bootstraps[125], bootstraps[4874]]
            if bootstraps
            else None,
            "preregistered_practical_advantage_observed": observed_advantage,
            "pairs": pairs,
        },
    }
