from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import BaseMessage, ToolMessage

from .jcs import sha256_json
from .publication import normalize_doi

AbstractEvidenceMode = Literal["legacy", "separate", "scoped"]
ABSTRACT_EVIDENCE_GUIDANCE = (
    "摘要候选未提供 relationship 表示没有预判科学方向，不是说摘要没有证据。"
    "比较性论断要核对原文实际比较的两组、结局及条件；组内改善不是组间优势。"
    "如果证据只及部分论断，指出该部分的支持或反驳及其边界，同时明确其余部分未获证明。"
    "若交付格式只允许对完整论断给一个方向，按完整论断给标签，"
    "把有依据的局部发现保留在说明里。正式引用资格不随这些判断改变。"
)

EVIDENCE_VIEW_VERSION = "parent-evidence-view-v1"
EVIDENCE_VIEW_GUIDANCE = (
    "证据视图保留原始摘要、来源与本榜名次。名次是相关性，不是科学方向。\n"
    "先看每篇确实测了什么、用了什么样本和条件，再判断它对论断的贡献。"
    "不同人群、方法、反证或改变结论的限定应分别体现，"
    "不以重复近邻塞满清单，也不只因名次靠后丢掉互补证据。\n"
    "同一 work_id 在多榜只算一篇；不拼接不同版本或来源的摘要。"
    "逐字引文从同篇原文取连续片段，模型理由只作线索。"
    "只有间接证据时，说明具体贡献和没测到的部分，不强判方向。\n"
    "citation_check 是正式引用资格，不是摘要对命题的方向标签。"
)

CANDIDATE_FIELDS = (
    "work_id",
    "paper_id",
    "result_id",
    "claim_id",
    "title",
    "doi",
    "year",
    "authors",
    "publication_types",
    "publication_status",
    "related_dois",
    "library_ids",
    "source_uri",
    "source_uris",
    "evidence_excerpt",
    "reason_evidence_quote",
    "base_rank",
    "llm_rank",
    "candidate_score",
    "score_version",
    "score_scale_group",
    "score_status",
    "ranking_status",
    "ranking_failure_code",
    "ranking_version",
    "pool_version",
)
REASON_FIELDS = (
    "status",
    "text",
    "served_model",
    "model",
    "alignment_score",
    "missing_constraints",
    "failure_code",
)
ABSTRACT_REFERENCE_GUIDANCE = (
    "abstract_ref 指向本次对话轮中同一 work_id 的逐字相同摘要。"
    "按 query_id、work_id 与 field 在已返回的回执中找到原文；引用原文，不引用标记。"
    "不同版本原文各自保留；名次、来源与出版状态以各自回执为准。"
)
WORKER_FIELDS = (
    "library_id",
    "worker_state",
    "index_snapshot",
    "source_agent_status",
    "candidates_seen",
    "candidates_verified",
    "source_candidates_submitted",
    "recommendations_emitted",
    "last_error",
)
RANKING_FIELDS = ("claim_id", "status", "pool_version", "ranking_version", "failure_code")


def _fields(row: dict, names: tuple[str, ...]) -> dict:
    return {key: row[key] for key in names if row.get(key) is not None}


def _source_records(item: dict) -> list[dict]:
    abstracts = {item["evidence_excerpt"]: "evidence_excerpt"}
    records = []
    for index, source in enumerate(item.get("source_records", [])):
        abstract = source["abstract"]
        if abstract in abstracts:
            row = {key: value for key, value in source.items() if key != "abstract"}
            row["abstract_ref"] = abstracts[abstract]
        else:
            row = dict(source)
            abstracts[abstract] = f"source_records[{index}].abstract"
        records.append(row)
    return records


def _candidate(item: dict) -> dict:
    projection = _fields(item, CANDIDATE_FIELDS)
    exclusions = item.get("rejection_reasons", [])
    projection["citation_check"] = {
        "citable": False,
        "scope_match": item.get("scope_match"),
        "exclusions": exclusions,
    }
    if "abstract_metadata_only" not in exclusions:
        projection["relationship"] = item.get("relationship")
    if reason := item.get("reason"):
        projection["reason"] = _fields(reason, REASON_FIELDS)
    if sources := _source_records(item):
        projection["source_records"] = sources
    return projection


def abstract_evidence_observation(result: dict, mode: AbstractEvidenceMode) -> dict:
    if mode == "legacy" or not (items := result.get("candidate_diagnostics")):
        return result
    candidates = []
    changed = False
    for item in items:
        if item.get("relationship") == "insufficient" and "abstract_metadata_only" in item.get(
            "rejection_reasons", ()
        ):
            candidates.append({key: value for key, value in item.items() if key != "relationship"})
            changed = True
        else:
            candidates.append(item)
    if not changed:
        return result
    updated = {**result, "candidate_diagnostics": candidates}
    if mode == "scoped":
        note = result.get("diagnostic_note") or ""
        updated["diagnostic_note"] = f"{note}\n{ABSTRACT_EVIDENCE_GUIDANCE}".lstrip("\n")
    return updated


def hide_rank_rationale(result: dict) -> dict:
    candidates = result.get("candidate_diagnostics", [])
    projected = []
    changed = False
    for candidate in candidates:
        reason = candidate.get("reason")
        if isinstance(reason, dict) and "text" in reason:
            projected.append(
                {
                    **candidate,
                    "reason": {k: v for k, v in reason.items() if k != "text"},
                }
            )
            changed = True
        else:
            projected.append(candidate)
    return {**result, "candidate_diagnostics": projected} if changed else result


def parent_abstract_references(messages: list[BaseMessage], *, tool_name: str) -> list[BaseMessage]:
    observed: dict[tuple[str, str], dict[str, str]] = {}
    projected = []
    for message in messages:
        if (
            isinstance(message, ToolMessage)
            and message.name == tool_name
            and isinstance(message.content, str)
        ):
            try:
                receipt = json.loads(message.content)
            except json.JSONDecodeError:
                receipt = None
            if isinstance(receipt, dict):
                compact = _abstract_reference_receipt(receipt, observed)
                if compact is not receipt:
                    message = message.model_copy(
                        update={"content": json.dumps(compact, ensure_ascii=False)}
                    )
        projected.append(message)
    return projected


def _abstract_reference_receipt(
    receipt: dict, observed: dict[tuple[str, str], dict[str, str]]
) -> dict:
    query = receipt.get("query_id")
    items = receipt.get("candidate_diagnostics") or []
    if (
        receipt.get("simulation") is not False
        or receipt.get("results")
        or not isinstance(query, str)
        or not items
        or not isinstance(items[0].get("claim_id"), str)
        or len({item.get("claim_id") for item in items}) != 1
        or len({item.get("work_id") for item in items}) != len(items)
        or any(
            "abstract_metadata_only"
            not in (
                item.get("rejection_reasons")
                or (item.get("citation_check") or {}).get("exclusions", [])
            )
            for item in items
        )
    ):
        return receipt
    candidates = []
    changed = False
    for item in items:
        work, text = item["work_id"], item.get("evidence_excerpt")
        if not isinstance(text, str) or not text:
            candidates.append(item)
            continue
        field = "evidence_excerpt"
        current = {"query_id": query, "work_id": work, "field": field}
        origin = observed.setdefault((work, text), current)
        entry = dict(item)
        if origin != current:
            del entry[field]
            entry["abstract_ref"] = origin
            changed = True
        if "source_records" in item:
            sources = []
            for source in item["source_records"]:
                if source.get("abstract") == text or source.get("abstract_ref") == field:
                    source = {k: v for k, v in source.items() if k != "abstract"}
                    source["abstract_ref"] = origin
                    changed = True
                sources.append(source)
            entry["source_records"] = sources
        candidates.append(entry)
    return {**receipt, "candidate_diagnostics": candidates} if changed else receipt


def _source_fingerprint(source: dict, *, abstract: str) -> str:
    return sha256_json(
        {
            "title": source.get("title") or "",
            "abstract": abstract,
            "authors": source.get("authors"),
            "year": source.get("year"),
            "doi": normalize_doi(source.get("doi")),
            "publication_status": source.get("publication_status", "unknown"),
            "publication_types": sorted(set(source.get("publication_types", []))),
            "related_dois": {
                relation: sorted(set(dois))
                for relation, dois in source.get("related_dois", {}).items()
                if dois
            },
        }
    )


class ParentEvidenceHistory:
    def __init__(self) -> None:
        self._observed: dict[str, set[str]] = {}
        self._completed_libraries: set[str] = set()
        self._has_receipt = False

    def observe(self, result: dict) -> dict | None:
        candidates = result.get("candidate_diagnostics", [])
        if (
            result.get("simulation") is not False
            or result.get("results")
            or any(
                "abstract_metadata_only" not in item.get("rejection_reasons", [])
                for item in candidates
            )
        ):
            return None
        present: dict[str, set[str]] = {}
        for item in candidates:
            records = item.get("source_records") or []
            fingerprints = (
                {_source_fingerprint(record, abstract=record["abstract"]) for record in records}
                if records
                else {_source_fingerprint(item, abstract=item.get("evidence_excerpt", ""))}
            )
            present.setdefault(item["work_id"], set()).update(fingerprints)

        new, changed = [], []
        for work_id, fingerprints in present.items():
            if work_id not in self._observed:
                new.append(work_id)
                self._observed[work_id] = fingerprints
            elif fingerprints - self._observed[work_id]:
                changed.append(work_id)
                self._observed[work_id].update(fingerprints)

        completed = set(result["coverage"]["completed_library_ids"])
        newly_completed = sorted(completed - self._completed_libraries)
        self._completed_libraries.update(completed)
        if not self._has_receipt:
            self._has_receipt = True
            return None
        status = (
            "unseen_evidence_returned"
            if new or changed
            else "no_unseen_evidence"
            if present
            else "no_candidates_returned"
        )
        summary = {
            "status": status,
            "new_work_count": len(new),
            "changed_work_count": len(changed),
            "unchanged_work_count": len(present) - len(new) - len(changed),
            "new_work_ids": new[:10],
            "changed_work_ids": changed[:10],
            "newly_completed_library_ids": newly_completed,
        }
        if status == "no_unseen_evidence":
            summary["note"] = (
                "相对本轮先前回执，未返回首次出现的论文或来源内容；不代表全库穷尽。"
                "证据足够可直接综合；若仍有实质缺口，可围绕缺少的对象、结局或文献类型补查。"
            )
        return summary


def evidence_observation(result: dict) -> dict:
    candidates = result.get("candidate_diagnostics")
    if not candidates:
        return result
    return {
        **result,
        "evidence_view_version": EVIDENCE_VIEW_VERSION,
        "candidate_diagnostics": [_candidate(item) for item in candidates],
        "candidate_rankings": [
            _fields(row, RANKING_FIELDS) for row in result["candidate_rankings"]
        ],
        "workers": [_fields(row, WORKER_FIELDS) for row in result["workers"]],
        "diagnostic_note": (
            "以下是待核查候选，摘要按来源完整保留。abstract_ref 引用同一候选内逐字相同的字段。"
            "引文资格、候选名次和模型理由都不能替代逐篇判断科学方向。"
            "该视图不改变榜单、来源、检索覆盖或快照。"
        ),
    }
