from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx
from tools.benchmark.core import ROOT, file_hash

from citation_tool.jcs import sha256_json

SUITE_ROOT = ROOT / "benchmark/gaps/scifact-v1"
RUNTIME = ROOT / "data/runtime/benchmark/gaps/scifact-v1"
SOURCE_URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
SELECTION_SEED = "literature-quality-gaps-scifact-v1-20261117"
MODEL = "deepseek-flash"


def save_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def raw_data() -> tuple[list[dict], list[dict], dict]:
    archive = RUNTIME / "source/data.tar.gz"
    if not archive.exists():
        response = httpx.get(SOURCE_URL, timeout=90, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > 32 * 1024 * 1024:
            raise ValueError("unexpectedly large SciFact archive")
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("xb") as stream:
            stream.write(response.content)
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as bundle:
        for name in ("corpus.jsonl", "claims_dev.jsonl"):
            matches = [
                entry
                for entry in bundle.getmembers()
                if entry.isfile() and Path(entry.name).name == name
            ]
            if len(matches) != 1 or matches[0].size > 32 * 1024 * 1024:
                raise ValueError(f"missing or ambiguous SciFact member: {name}")
            reader = bundle.extractfile(matches[0])
            if reader is None:
                raise ValueError("cannot read SciFact member")
            contents[name] = reader.read()
    records = [json.loads(line) for line in contents["corpus.jsonl"].splitlines() if line]
    claims = [json.loads(line) for line in contents["claims_dev.jsonl"].splitlines() if line]
    if len(records) != 5183 or len(claims) != 300:
        raise ValueError("original SciFact dev/corpus sizes differ from the frozen protocol")
    provenance = {
        "source_url": SOURCE_URL,
        "archive_sha256": file_hash(archive),
        "members_sha256": {
            name: hashlib.sha256(value).hexdigest() for name, value in contents.items()
        },
        "source_license": {
            "url": "https://github.com/allenai/scifact/blob/master/LICENSE.md",
            "claims_and_annotations": "CC BY 4.0",
            "abstracts": "ODC-By 1.0",
        },
    }
    return records, claims, provenance


def normalized_corpus(raw: list[dict]) -> dict:
    records = [
        {
            "work_id": f"work:scifact:{record['doc_id']}",
            "paper_id": f"scifact:{record['doc_id']}",
            "doc_id": str(record["doc_id"]),
            "title": record["title"],
            "abstract": " ".join(record["abstract"]),
            "abstract_sentences": record["abstract"],
            "doi": None,
            "year": None,
            "authors": None,
            "pub_types": [],
            "retraction_status": "unknown",
            "simulation": False,
            "evidence_level": "abstract_metadata_only",
            "source_metadata_url": f"{SOURCE_URL}#doc_id={record['doc_id']}",
        }
        for record in raw
    ]
    if len({row["work_id"] for row in records}) != len(records):
        raise ValueError("duplicate SciFact document identity")
    return {
        "corpus_id": "scifact-original-5183",
        "corpus_sha256": sha256_json(records),
        "citation_scope": "real titles/abstracts; no DOI/year supplied; not full-text evidence",
        "governance_scope": "publication status not verified",
        "records": records,
    }


def label(claim: dict) -> str:
    labels = {item["label"] for values in claim["evidence"].values() for item in values}
    return next(iter(labels)) if len(labels) == 1 else "MIXED" if labels else "NEI"


def _order(claim: dict) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}:{claim['id']}".encode()).hexdigest()


def select_claims(claims: list[dict]) -> list[tuple[dict, str, str]]:
    ordered = sorted(claims, key=_order)
    selected = []
    used_docs = set()
    used_claims = set()
    quotas = (("multi_document", 4), ("support", 8), ("contradict", 8), ("evidence_gap", 4))
    for slice_name, count in quotas:
        chosen = 0
        for claim in ordered:
            evidence_count = len(claim["evidence"])
            matches = {
                "multi_document": evidence_count >= 2,
                "support": evidence_count == 1 and label(claim) == "SUPPORT",
                "contradict": evidence_count == 1 and label(claim) == "CONTRADICT",
                "evidence_gap": evidence_count == 0,
            }[slice_name]
            docs = set(claim["cited_doc_ids"])
            if not matches or not docs or used_docs & docs:
                continue
            family = "sf-family-" + sha256_json(sorted(docs))[:12]
            selected.append((claim, slice_name, family))
            used_claims.add(claim["id"])
            used_docs.update(docs)
            chosen += 1
            if chosen == count:
                break
        if chosen != count:
            raise ValueError(f"SciFact stratum {slice_name} has only {chosen}/{count} families")
    families = defaultdict(list)
    for claim in ordered:
        if claim["id"] not in used_claims and not used_docs & set(claim["cited_doc_ids"]):
            families[tuple(sorted(claim["cited_doc_ids"]))].append(claim)
    pairs = 0
    for docs, members in sorted(families.items(), key=lambda item: sha256_json(item[0])):
        support = next((claim for claim in members if label(claim) == "SUPPORT"), None)
        contradict = next((claim for claim in members if label(claim) == "CONTRADICT"), None)
        if support is None or contradict is None or used_docs & set(docs):
            continue
        family = "sf-polarity-" + sha256_json(list(docs))[:12]
        selected.extend((claim, "polarity_pair", family) for claim in (support, contradict))
        used_docs.update(docs)
        pairs += 1
        if pairs == 2:
            break
    if pairs != 2:
        raise ValueError("SciFact lacks two disjoint polarity pairs for the frozen rule")
    return selected


OUTPUT_INSTRUCTION = (
    "在本次提供的题录摘要库内找能支持或反驳以下论断的已有研究。"
    "不要只找支持论文；反驳也是重要候选。最多列10篇最相关文献，按研究价值排序；"
    "近邻但不能判断者可标 INSUFFICIENT，没有可靠证据可以给空列表。"
    "只能根据本次实际读到的题名摘要，不用外部论文补齐，不补造 DOI、年份、作者或全文位置。"
    "doc_id 使用返回的 work:scifact:数字 / scifact:数字 中的数字 ID。"
    "quote 必须是该篇摘要中逐字连续的一段（保留大小写与标点）；"
    "reason 用中文说明该片段与论断的关系。不要把主题相关冒充支持或反驳。"
    "最终只返回一个 JSON 对象，不加 Markdown："
    '{"papers":[{"doc_id":"数字","stance":"SUPPORT|CONTRADICT|INSUFFICIENT",'
    '"quote":"逐字摘要片段","reason":"理由与边界"}],"conclusion":"摘要证据层面的结论与限制"}。'
    "无需服从固定检索流程，可以自主改写查询、补查或停止；质量优先，不以轮次少为目标。"
)


def make_task(claim: dict, slice_name: str, family: str, corpus: dict) -> dict:
    by_id = {row["doc_id"]: row for row in corpus["records"]}
    evidence = claim["evidence"]
    for doc_id, rationales in evidence.items():
        sentences = by_id[doc_id]["abstract_sentences"]
        for rationale in rationales:
            if rationale["label"] not in {"SUPPORT", "CONTRADICT"} or not all(
                0 <= index < len(sentences) for index in rationale["sentences"]
            ):
                raise ValueError("invalid original SciFact evidence")
    query = f"{OUTPUT_INSTRUCTION}\n\n待核查论断：{claim['claim']}"
    return {
        "case_id": f"sf-{claim['id']}",
        "run_case_id": f"sf-{claim['id']}",
        "repetition": 1,
        "split": "holdout",
        "slice": slice_name,
        "family": family,
        "claim_id": claim["id"],
        "claim": claim["claim"],
        "baseline_query": query,
        "request": {"input_type": "abstract", "abstract": claim["claim"]},
        "retriever": "hybrid",
        "dense_threshold": 0.80,
        "expect": {"evidence": evidence, "cited_doc_ids": claim["cited_doc_ids"]},
    }


def prepare() -> dict:
    output = SUITE_ROOT / "tasks.json"
    if output.exists():
        suite, corpus = load_data()
        return {"status": "existing_frozen_suite", "tasks": len(suite["tasks"]), **suite["source"]}
    raw, claims, source = raw_data()
    corpus = normalized_corpus(raw)
    selections = select_claims(claims)
    suite = {
        "suite_id": "literature-gaps-scifact-v1",
        "selection_seed": SELECTION_SEED,
        "source": source,
        "protocol_sha256": file_hash(SUITE_ROOT / "protocol.md"),
        "corpus_sha256": corpus["corpus_sha256"],
        "claim_count": len(claims),
        "tasks": [make_task(*item, corpus) for item in selections],
        "all_claims": claims,
    }
    save_new(RUNTIME / "corpus.json", corpus)
    save_new(output, suite)
    return {
        "status": "prepared_without_model_calls",
        "records": len(corpus["records"]),
        "tasks": len(suite["tasks"]),
        "slices": dict(Counter(task["slice"] for task in suite["tasks"])),
        "corpus_sha256": corpus["corpus_sha256"],
        **source,
    }


def load_data() -> tuple[dict, dict]:
    suite = json.loads((SUITE_ROOT / "tasks.json").read_text(encoding="utf-8"))
    corpus = json.loads((RUNTIME / "corpus.json").read_text(encoding="utf-8"))
    if suite["corpus_sha256"] != sha256_json(corpus["records"]) or suite[
        "protocol_sha256"
    ] != file_hash(SUITE_ROOT / "protocol.md"):
        raise ValueError("frozen SciFact protocol or corpus changed")
    return suite, corpus
