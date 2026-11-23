from __future__ import annotations

import re
from collections import Counter
from statistics import fmean

import numpy as np

from .metrics import parse_answer

HIGHER_IS_BETTER = ("completed", "valid_format", "recall_at_10", "full_rationale_coverage")
MUST_NOT_INCREASE = ("opposite", "unsupplied_id", "invalid_quote", "duplicate_id")
PRIMARY = "directional_coverage"


def score(record: dict, gold: dict, sources: dict, supplied: set[str]) -> dict:
    answer, _ = parse_answer(record.get("response_text") or "")
    completed = bool(record["completed"] and record["model_valid"] and not record["secret_leak"])
    counts = Counter({key: 0 for key in (*MUST_NOT_INCREASE, "insufficient", "unjudged_direction")})
    seen, recalled, covered, full = set(), set(), set(), set()
    assertions = []
    for paper in answer.papers if answer is not None else ():
        doc = paper.doc_id
        source = sources.get(doc)
        eligible = source is not None and doc in supplied
        exact = bool(
            eligible
            and len(re.findall(r"\w+", paper.quote)) >= 4
            and paper.quote in source["abstract"]
        )
        counts["unsupplied_id"] += not eligible
        counts["invalid_quote"] += not exact
        if doc in seen:
            counts["duplicate_id"] += 1
            continue
        seen.add(doc)
        rationales = gold.get(doc)
        if not rationales:
            if eligible and paper.stance != "INSUFFICIENT":
                counts["unjudged_direction"] += 1
                assertions.append({"id": doc, "stance": paper.stance})
            continue
        if not eligible:
            continue
        recalled.add(doc)
        matching = [r for r in rationales if r["label"] == paper.stance]
        if not matching:
            counts["insufficient" if paper.stance == "INSUFFICIENT" else "opposite"] += 1
        if matching and exact:
            covered.add(doc)
            if any(
                r["sentences"]
                and all(source["abstract_sentences"][sid] in paper.quote for sid in r["sentences"])
                for r in matching
            ):
                full.add(doc)
    denominator = len(gold)
    result = {"completed": completed, "valid_format": answer is not None, **counts}
    if denominator:
        result.update(
            supplied_recall=len(supplied & gold.keys()) / denominator,
            recall_at_10=len(recalled) / denominator if completed else 0.0,
            directional_coverage=len(covered) / denominator if completed else 0.0,
            full_rationale_coverage=len(full) / denominator if completed else 0.0,
        )
    else:
        result.update(
            dict.fromkeys(("supplied_recall", "recall_at_10", PRIMARY, "full_rationale_coverage"))
        )
    return {**result, "targets": denominator, "unjudged_assertions": assertions}


def _aggregate(rows: list[dict]) -> dict:
    pos = [row for row in rows if row["targets"]]
    return {
        "runs": len(rows),
        "positive_families": len(pos),
        **{
            key: sum(row[key] for row in rows)
            for key in (
                "completed",
                "valid_format",
                *MUST_NOT_INCREASE,
                "unjudged_direction",
                "insufficient",
            )
        },
        **{
            key: fmean(row[key] for row in pos) if pos else None
            for key in ("supplied_recall", "recall_at_10", PRIMARY, "full_rationale_coverage")
        },
    }


def compare(rows: list[dict], arms: tuple[str, ...]) -> dict:
    if len({row["family"] for row in rows}) != len(rows) or any(
        set(row["arms"]) != set(arms) for row in rows
    ):
        raise ValueError("assessment requires one independent, complete pair per family")
    summaries = {arm: _aggregate([row["arms"][arm] for row in rows]) for arm in arms}
    baseline = summaries["legacy"]
    paired = {}
    for arm in arms[1:]:
        alt = summaries[arm]
        deltas = [
            row["arms"][arm][PRIMARY] - row["arms"]["legacy"][PRIMARY]
            for row in rows
            if row["arms"]["legacy"]["targets"]
        ]
        if not deltas:
            raise ValueError("no gold-labeled evidence families in this cohort")
        bootstrap = np.random.default_rng(20261119).choice(deltas, size=(10000, len(deltas)))
        blockers = [key for key in HIGHER_IS_BETTER if alt[key] < baseline[key] - 1e-9]
        blockers.extend(key for key in MUST_NOT_INCREASE if alt[key] > baseline[key])
        if alt["unjudged_direction"] > baseline["unjudged_direction"]:
            blockers.append("unjudged_direction_increase_requires_independent_review")
        paired[arm] = {
            "delta": fmean(deltas),
            "interval_95": np.quantile(bootstrap.mean(axis=1), [0.025, 0.975]).tolist(),
            "wins": sum(value > 0 for value in deltas),
            "ties": sum(value == 0 for value in deltas),
            "losses": sum(value < 0 for value in deltas),
            "guardrail_blockers": blockers,
        }
    return {
        "arms": summaries,
        "paired": paired,
        "strata": {
            stratum: {
                arm: _aggregate([row["arms"][arm] for row in rows if row["stratum"] == stratum])
                for arm in arms
            }
            for stratum in sorted({row["stratum"] for row in rows})
        },
        "cases": rows,
    }


def select_variant(assessment: dict) -> str | None:
    choices = [
        (name, row["delta"])
        for name, row in assessment["paired"].items()
        if row["delta"] >= 0.05 - 1e-9 and not row["guardrail_blockers"]
    ]
    return max(choices, key=lambda item: (item[1], item[0] == "separate"))[0] if choices else None


def test_passed(assessment: dict, arm: str) -> bool:
    row = assessment["paired"][arm]
    return row["delta"] >= 0.05 - 1e-9 and row["wins"] >= 3 and not row["guardrail_blockers"]
