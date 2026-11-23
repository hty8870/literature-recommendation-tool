"""Reproducible retrieval-quality A/B on the frozen SciFact dev claims with no model calls.

The "before" arm embeds a frozen copy of the pre-fix query interpretation, so the
comparison can be re-run after the fix without a VCS checkout. The "after" arm calls
the production functions; its replicated ranking is checked against the real
``SemanticMetadataAdapter`` for every claim. The single-route protection arms are
closeable experiments and are not wired into production.

Usage:
    uv run python -m tools.retrieval_quality_probe --name <new-name>
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path

from tools.benchmark.core import ROOT, file_hash
from tools.gap_benchmark.data import SUITE_ROOT, label, load_data
from tools.quality_benchmark.core import EMBEDDING_CACHE_DIR

from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import ClaimContext, SearchCreate, utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.reasons import DESIGN_ATTRIBUTE_PATTERNS, _has_explicit_design
from citation_tool.relevance import (
    STOPWORDS,
    TOPIC_ALIASES,
    QueryPlan,
    QueryRelevance,
    query_plan,
    record_allowed,
    score_records,
    semantic_query_text,
)
from citation_tool.search import _build_claim
from citation_tool.semantic import SemanticMetadataAdapter

TOP_K = 20
DENSE_THRESHOLD = 0.80
RRF_K = 60
OUTPUT_ROOT = ROOT / "data/runtime/retrieval-quality"
BASELINE_ENGINE = SUITE_ROOT / "results/public-gap-first-20261117-v1/engine.json"

LEGACY_NEGATION = re.compile(
    r"without|excluding|exclude|except|instead of|rather than|"
    r"(?<![a-z])not(?! only\b)(?![a-z])|"
    r"不要把|不要|不包括|排除|而非|而不是|不涉及|未测定|没有测量"
)
LEGACY_RANDOMIZED = (
    r"(?<![a-z])(?:randomized|randomised|randomly (?:assigned|allocated)|"
    r"randomization|randomisation|random allocation)(?![a-z])|"
    r"随机(?:化|分配|对照|试验|入组)?(?!森林|变量|效应|噪声|矩阵|采样)"
)
DESIGN_CASES = (
    ("该研究采用随机模拟估计参数。", False),
    ("该模型使用随机表达描述细胞间噪声。", False),
    ("随机过程驱动表达波动。", False),
    ("随机噪声导致测量差异。", False),
    ("The randomized simulation used stochastic rounding.", False),
    ("A randomized algorithm was benchmarked.", False),
    ("We used randomization tests for significance.", False),
    ("该研究为随机对照试验。", True),
    ("该研究为随机、双盲、安慰剂对照试验。", True),
    ("采用随机、对照设计。", True),
    ("随机、确定性模型都用于预测。", False),
    ("患者被随机分配至两组。", True),
    ("研究采用随机化设计。", True),
    ("随机分组后随访 12 周。", True),
    ("A randomized, double-blind, placebo-controlled trial.", True),
    ("Participants were randomly assigned to placebo.", True),
    ("Allocation used random allocation sequences.", True),
)


def _legacy_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 64) : start].casefold()
    cues = list(LEGACY_NEGATION.finditer(prefix))
    if not cues:
        return False
    between = prefix[cues[-1].end() :].strip()
    if len(between) > 32 or re.search(r"[。.!?;；，,]", between):
        return False
    return not re.search(r"研究|论文|文献|主题|工作", between)


def _legacy_query_terms(text: str) -> list[str]:
    folded = text.casefold()
    return [
        match.group().casefold()
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9-]*", text)
        if len(match.group()) >= 3
        and match.group().casefold() not in STOPWORDS
        and not _legacy_negated(folded, match.start())
    ]


def _legacy_semantic_query_text(claim: ClaimContext) -> str:
    if claim.input_type == "keywords" and claim.query_terms:
        terms = [term for term in claim.query_terms if not LEGACY_NEGATION.search(term.casefold())]
        return " ".join(terms)
    fragments = []
    for sentence in re.split(r"(?<=[.!?。！？])\s+", claim.claim_text):
        negation = LEGACY_NEGATION.search(sentence)
        if negation is None:
            fragments.append(sentence)
            continue
        prefix = sentence[: negation.start()].strip(" ,;，；")
        if prefix:
            fragments.append(prefix)
    return " ".join(fragments).strip()


def _legacy_plan(claim: ClaimContext) -> QueryPlan:
    text = f"{claim.claim_text} {' '.join(claim.query_terms)}"
    folded = text.casefold()
    identifiers, excluded_identifiers = [], []
    doi_spans = [match.span() for match in re.finditer(r"10\.\d{4,9}/[^\s，。；]+", folded)]
    for match in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*", folded):
        if any(start <= match.start() and match.end() <= end for start, end in doi_spans):
            continue
        if _legacy_negated(folded, match.start()):
            excluded_identifiers.append(match.group())
        else:
            identifiers.append(match.group())
    positive_topics, excluded_topics = [], []
    for concept, aliases in TOPIC_ALIASES.items():
        occurrences = [
            (match.group(), match.start())
            for alias in aliases
            for match in re.finditer(re.escape(alias), folded)
        ]
        if not occurrences:
            continue
        if all(_legacy_negated(folded, start) for _, start in occurrences):
            excluded_topics.append(concept)
        elif any(not _legacy_negated(folded, start) for _, start in occurrences):
            positive_topics.append(concept)
    terms = sorted(set(_legacy_query_terms(text) + sorted(set(identifiers)) + positive_topics))
    doi = re.search(r"10\.\d{4,9}/[^\s，。；]+", text, re.IGNORECASE)
    return QueryPlan(
        text=text,
        terms=tuple(terms),
        identifiers=tuple(sorted(set(identifiers))),
        topics=tuple(positive_topics),
        excluded_topics=tuple(excluded_topics),
        excluded_identifiers=tuple(sorted(set(excluded_identifiers))),
        doi=doi.group().rstrip(".,;:!?") if doi else None,
    )


def _sparse(claim: ClaimContext, plan: QueryPlan, index, records) -> dict[str, QueryRelevance]:
    matches = score_records(claim, records, index=index, plan=plan)
    known = {work_id for work_id, _title, _text, _doi in records}
    return {work_id: matches[work_id] for work_id in matches if work_id in known}


def _dense(claim: ClaimContext, plan: QueryPlan, index, records, query_text: str):
    similarities = index.similarities(claim, query_text=query_text)
    return {
        work_id: score
        for work_id, title, text, doi in records
        if (score := similarities.get(work_id, 0.0)) >= DENSE_THRESHOLD
        and record_allowed(claim, title, text, doi, plan=plan)
    }


def _ranks(values: dict[str, float]) -> dict[str, int]:
    order = sorted(values, key=lambda key: (-values[key], key))[:TOP_K]
    return {work_id: rank for rank, work_id in enumerate(order, 1)}


def _fused_scores(
    sparse: dict[str, QueryRelevance],
    dense: dict[str, float],
    *,
    protect_top: int | None = None,
) -> dict[str, float]:
    sparse_ranks = _ranks({key: value.score for key, value in sparse.items()})
    dense_ranks = _ranks(dense)
    scores = {}
    for work_id in sorted(set(sparse_ranks) | set(dense_ranks)):
        ranks = [sparse_ranks.get(work_id), dense_ranks.get(work_id)]
        contributions = [1 / (RRF_K + rank) for rank in ranks if rank]
        if protect_top is not None:
            present = [rank for rank in ranks if rank]
            if len(present) == 1 and present[0] <= protect_top:
                contributions.append(1 / (RRF_K + TOP_K))
        scores[work_id] = 100 * sum(contributions) / (2 / (RRF_K + 1))
    return scores


def _ordered(scores: dict[str, float], limit: int = TOP_K) -> list[str]:
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]


def _union_top5(ordered: list[str], sparse, dense, limit: int = TOP_K) -> list[str]:
    required = list(_ordered({key: value.score for key, value in sparse.items()}, 5)) + list(
        _ordered(dense, 5)
    )
    merged = list(ordered)
    for work_id in required:
        if work_id not in merged:
            merged.append(work_id)
    return merged[:limit]


def _metrics(ids: list[str], evidence: dict, cutoff: int) -> dict:
    ranked = list(dict.fromkeys(ids))[:cutoff]
    targets = set(evidence)
    retrieved = targets & set(ranked)
    ideal = sum(1 / math.log2(index + 2) for index in range(min(len(targets), cutoff)))
    dcg = sum(1 / math.log2(index + 2) for index, value in enumerate(ranked) if value in targets)
    return {
        "recall": len(retrieved) / len(targets) if targets else None,
        "ndcg": dcg / ideal if ideal else None,
    }


def _mean(values) -> float | None:
    available = [value for value in values if value is not None]
    return statistics.fmean(available) if available else None


def _gold_filtered(claim: ClaimContext, plan: QueryPlan, by_id: dict[str, dict]) -> list[str]:
    filtered = []
    for doc_id in claim["evidence"]:
        source = by_id.get(f"work:scifact:{doc_id}")
        if source is None:
            continue
        if not record_allowed(claim, source["title"], source["abstract"], source["doi"], plan=plan):
            filtered.append(doc_id)
    return filtered


def run(name: str, limit: int) -> dict:
    output = OUTPUT_ROOT / name
    if output.exists():
        raise FileExistsError(output)
    suite, corpus = load_data()
    frozen_engine = json.loads(BASELINE_ENGINE.read_text(encoding="utf-8"))
    frozen = {
        (row["claim_id"], row["method"]): row["candidate_ids"] for row in frozen_engine["cases"]
    }
    records = corpus["records"]
    tuples = [(row["work_id"], row["title"], row["abstract"], row["doi"]) for row in records]
    by_id = {row["work_id"]: row for row in records}
    library = LibraryConfig("scifact", "SciFact original abstracts", "fast", 1, simulation=False)
    adapter = SemanticMetadataAdapter(
        library,
        records,
        settings=Settings(embedding_cache_dir=EMBEDDING_CACHE_DIR),
        dense_threshold=DENSE_THRESHOLD,
        top_k=TOP_K,
    )
    adapter._index._load_matrix()
    rows = []
    for claim in suite["all_claims"][:limit]:
        query, _ = _build_claim(
            "retrieval-quality",
            f"sf-{claim['id']}",
            SearchCreate(input_type="abstract", abstract=claim["claim"]),
            utc_now_iso(),
            None,
        )
        legacy = _legacy_plan(query)
        current = query_plan(query)
        arms = {}
        sparse_legacy = _sparse(query, legacy, adapter._lexical_index, tuples)
        dense_legacy = _dense(
            query, legacy, adapter._index, tuples, _legacy_semantic_query_text(query)
        )
        sparse_after = _sparse(query, current, adapter._lexical_index, tuples)
        dense_after = _dense(query, current, adapter._index, tuples, semantic_query_text(query))
        arms["before"] = _ordered(_fused_scores(sparse_legacy, dense_legacy))
        after_fused = _fused_scores(sparse_after, dense_after)
        arms["after"] = _ordered(after_fused)
        arms["after_protected"] = _ordered(_fused_scores(sparse_after, dense_after, protect_top=3))
        arms["after_union5"] = _union_top5(arms["after"], sparse_after, dense_after)
        arms = {
            arm: [work_id.removeprefix("work:scifact:") for work_id in ids]
            for arm, ids in arms.items()
        }
        bm25_before = [
            work_id.removeprefix("work:scifact:")
            for work_id in _ordered({key: value.score for key, value in sparse_legacy.items()})
        ]
        bm25_after = [
            work_id.removeprefix("work:scifact:")
            for work_id in _ordered({key: value.score for key, value in sparse_after.items()})
        ]
        production = [
            row.passage.work_id.removeprefix("work:scifact:") for row in adapter.search(query)
        ]
        rows.append(
            {
                "claim_id": claim["id"],
                "label": label(claim),
                "evidence_documents": len(claim["evidence"]),
                "targets": sorted(claim["evidence"]),
                "before": arms["before"],
                "after": arms["after"],
                "after_protected": arms["after_protected"],
                "after_union5": arms["after_union5"],
                "bm25_before": bm25_before,
                "bm25_after": bm25_after,
                "production_matches_after": production == arms["after"],
                "frozen_engine_matches_before": frozen.get((claim["id"], "hybrid_rrf"))
                == arms["before"]
                and frozen.get((claim["id"], "bm25")) == bm25_before,
                "legacy_semantic_query": _legacy_semantic_query_text(query),
                "current_semantic_query": semantic_query_text(query),
                "legacy_excluded_identifiers": list(legacy.excluded_identifiers),
                "current_excluded_identifiers": list(current.excluded_identifiers),
                "language_changed": _legacy_semantic_query_text(query) != semantic_query_text(query)
                or legacy.excluded_identifiers != current.excluded_identifiers,
                "gold_filtered_before": _gold_filtered(claim, legacy, by_id),
                "gold_filtered_after": _gold_filtered(claim, current, by_id),
            }
        )
        if len(rows) % 50 == 0:
            print(f"claims={len(rows)}/{min(limit, len(suite['all_claims']))}", flush=True)
    return summarize(name, rows, frozen_engine)


def _summary(rows: list[dict], arms: tuple[str, ...]) -> dict:
    out = {}
    for arm in arms:
        recalls_5, recalls_20, ndcgs = [], [], []
        for row in rows:
            evidence = {doc_id: [] for doc_id in row["targets"]}
            if not evidence:
                continue
            metrics_5 = _metrics(row[arm], evidence, 5)
            metrics_20 = _metrics(row[arm], evidence, 20)
            recalls_5.append(metrics_5["recall"])
            recalls_20.append(metrics_20["recall"])
            ndcgs.append(metrics_5["ndcg"])
        out[arm] = {
            "positive_claims": len(recalls_5),
            "recall_at_5": _mean(recalls_5),
            "recall_at_20": _mean(recalls_20),
            "ndcg_at_5": _mean(ndcgs),
            "zero_recall_at_5": sum(value == 0 for value in recalls_5),
        }
    return out


def summarize(name: str, rows: list[dict], frozen_engine: dict) -> dict:
    positives = [row for row in rows if row["targets"]]
    changed = [row for row in positives if row["language_changed"]]
    unchanged = [row for row in positives if not row["language_changed"]]
    arms = ("before", "after", "after_protected", "after_union5")
    result = {
        "schema": "retrieval-quality-probe-v1",
        "created_at": utc_now_iso(),
        "name": name,
        "scope": (
            "development A/B on all 300 SciFact dev claims; official evidence only; "
            "no model calls; public old data is not an unseen holdout"
        ),
        "implementation_hash": sha256_json(
            {
                path.name: file_hash(path)
                for path in (
                    ROOT / "src/citation_tool/relevance.py",
                    ROOT / "src/citation_tool/semantic.py",
                    ROOT / "src/citation_tool/reasons.py",
                    ROOT / "tools/retrieval_quality_probe.py",
                )
            }
        ),
        "frozen_engine_sha256": file_hash(BASELINE_ENGINE),
        "frozen_engine_created_at": frozen_engine["created_at"],
        "claims": len(rows),
        "positive_claims": len(positives),
        "systems": _summary(positives, arms),
        "bm25": _summary(positives, ("bm25_before", "bm25_after")),
        "slices": {
            "negation_affected": _summary(changed, arms),
            "negation_clean": _summary(unchanged, arms),
            "multi_document": _summary(
                [row for row in positives if row["evidence_documents"] > 1], arms
            ),
            "single_document": _summary(
                [row for row in positives if row["evidence_documents"] == 1], arms
            ),
        },
        "integrity": {
            "production_matches_after": sum(row["production_matches_after"] for row in rows),
            "frozen_engine_matches_before": sum(
                row["frozen_engine_matches_before"] for row in rows
            ),
            "clean_slice_identical_before_after": sum(
                row["before"] == row["after"] for row in unchanged
            ),
            "clean_slice_total": len(unchanged),
            "gold_filtered_before": sorted(
                {
                    row["claim_id"]: row["gold_filtered_before"]
                    for row in rows
                    if row["gold_filtered_before"]
                }.items()
            ),
            "gold_filtered_after": sorted(
                {
                    row["claim_id"]: row["gold_filtered_after"]
                    for row in rows
                    if row["gold_filtered_after"]
                }.items()
            ),
            "changed_claims": [
                {
                    "claim_id": row["claim_id"],
                    "legacy": row["legacy_semantic_query"],
                    "current": row["current_semantic_query"],
                    "legacy_excluded_identifiers": row["legacy_excluded_identifiers"],
                }
                for row in changed
            ],
        },
        "design_attribute_probe": design_probe(),
        "rows": rows,
    }
    return result


def design_probe() -> dict:
    def score(pattern: str) -> dict:
        true_positive = false_positive = false_negative = 0
        misses = []
        for text, expected in DESIGN_CASES:
            detected = _has_explicit_design(text, pattern)
            if detected and expected:
                true_positive += 1
            elif detected and not expected:
                false_positive += 1
                misses.append({"text": text, "kind": "false_positive"})
            elif expected and not detected:
                false_negative += 1
                misses.append({"text": text, "kind": "false_negative"})
        return {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "misses": misses,
        }

    return {
        "cases": [
            {"text": text, "expected_randomized": expected} for text, expected in DESIGN_CASES
        ],
        "before_pattern": score(LEGACY_RANDOMIZED),
        "after_pattern": score(DESIGN_ATTRIBUTE_PATTERNS["randomized"]),
        "note": "deterministic labeled micro-suite; historical rejected reasons were not persisted",
    }


def render(result: dict) -> str:
    def number(value) -> str:
        return "—" if value is None else f"{value:.4f}"

    def table(rows: dict) -> str:
        lines = [
            "| 臂 | 正例 | Recall@5 | Recall@20 | nDCG@5 | Top5 零召回 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for arm, value in rows.items():
            lines.append(
                f"| {arm} | {value['positive_claims']} | {number(value['recall_at_5'])} | "
                f"{number(value['recall_at_20'])} | {number(value['ndcg_at_5'])} | "
                f"{value['zero_recall_at_5']} |"
            )
        return "\n".join(lines)

    integrity = result["integrity"]
    design = result["design_attribute_probe"]
    production = integrity["production_matches_after"]
    frozen = integrity["frozen_engine_matches_before"]
    clean = integrity["clean_slice_identical_before_after"]
    clean_total = integrity["clean_slice_total"]
    parts = [
        f"# 检索质量探针 `{result['name']}`",
        "",
        "开发对照：固定 SciFact 原始题录/摘要与预建向量缓存，无真实模型调用；"
        "公开旧数据不称未见留出。",
        "",
        "## 总体（官方证据正例）",
        "",
        table(result["systems"]),
        "",
        "## BM25-only",
        "",
        table(result["bm25"]),
        "",
        "## 切片",
        "",
    ]
    for slice_name, values in result["slices"].items():
        parts.extend([f"### {slice_name}", "", table(values), ""])
    parts.extend(
        [
            "## 完整性与排除变化",
            "",
            f"- 生产适配器与复算 after 一致：{production}/{result['claims']}",
            f"- 改前复算与冻结 engine.json 一致：{frozen}/{result['claims']}",
            f"- 未受影响切片改前/改后候选完全相同：{clean}/{clean_total}",
            f"- 改前被排除的金标：{integrity['gold_filtered_before']}",
            f"- 改后被排除的金标：{integrity['gold_filtered_after']}",
            "",
            "## 研究设计属性识别",
            "",
            f"- 改前：{design['before_pattern']['false_positive']} 误报 / "
            f"{design['before_pattern']['false_negative']} 漏报",
            f"- 改后：{design['after_pattern']['false_positive']} 误报 / "
            f"{design['after_pattern']['false_negative']} 漏报",
            "",
        ]
    )
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SciFact retrieval-quality A/B probe without model calls"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.name):
        raise ValueError("probe name must not contain paths")
    result = run(args.name, args.limit)
    target = args.output or (OUTPUT_ROOT / args.name / "probe.json")
    resolved = target.resolve()
    if not resolved.is_relative_to(ROOT / "data/runtime"):
        raise ValueError("probe output must stay in ignored runtime data")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    report = resolved.with_name("report.md")
    with report.open("x", encoding="utf-8") as stream:
        stream.write(render(result) + "\n")
    print(
        json.dumps(
            {
                "systems": result["systems"],
                "integrity": {
                    key: value
                    for key, value in result["integrity"].items()
                    if not isinstance(value, list)
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
