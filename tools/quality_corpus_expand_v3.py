"""Build a resumable, versioned Europe PMC title/abstract corpus."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from citation_tool.entities import utc_now_iso
from citation_tool.publication import canonical_work_id, normalize_doi

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data/runtime/quality-corpus"
PUBLIC = ROOT / "benchmark/quality"
ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TOPICS = (
    "soybean",
    "rice",
    "wheat",
    "maize",
    "arabidopsis",
    "plant flowering",
    "floral organ",
    "root nodule",
    "plant immunity",
    "plant drought",
    "salt stress plants",
    "photosynthesis",
    "CRISPR plants",
    "plant pangenome",
    "plant single-cell",
    "plant epigenomics",
    "plant RNA modification",
    "crop yield",
    "seed development",
    "fruit ripening",
    "plant hormone",
    "biological nitrogen fixation",
    "mycorrhiza",
    "plant microbiome",
    "plant pathology",
    "crop disease resistance",
    "crop genome editing",
    "plant transcriptomics",
    "plant proteomics",
    "plant metabolomics",
    "plant abiotic stress",
    "plant secondary metabolism",
    "crop domestication",
    "heterosis",
    "plant reproduction",
    "pollen development",
    "seed dormancy",
    "plant mitochondria",
    "chloroplast",
    "plant long noncoding RNA",
    "plant DNA methylation",
    "plant histone modification",
    "plant alternative splicing",
    "plant circadian rhythm",
    "plant temperature stress",
    "plant phosphorus",
    "plant potassium",
    "plant water use efficiency",
    "plant cell wall",
    "plant vascular development",
    "plant meristem",
    "plant regeneration",
    "plant genome assembly",
    "plant systems biology",
    "cancer single-cell",
    "spatial transcriptomics",
    "protein structure prediction",
    "metagenomics",
    "gene therapy",
    "CRISPR diagnostics",
    "RNA interference",
    "m6A RNA",
    "genome assembly",
    "variant calling",
    "protein engineering",
)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"<[^>]+>", "", html.unescape(value)).strip()


def normalize_record(item: dict[str, Any]) -> dict[str, Any] | None:
    doi = normalize_doi(item.get("doi"))
    title = clean_text(item.get("title"))
    abstract = clean_text(item.get("abstractText"))
    if not doi or not title or len(abstract) < 300:
        return None
    work_id = canonical_work_id({"doi": doi})
    source_id = item.get("pmid") or item.get("id") or work_id
    year = item.get("pubYear")
    return {
        "work_id": work_id,
        "paper_id": f"paper:europepmc:{source_id}",
        "library_id": "europepmc-real",
        "simulation": False,
        "evidence_level": "abstract_metadata_only",
        "title": title,
        "year": int(year) if isinstance(year, str) and year.isdigit() else None,
        "doi": doi,
        "authors": item.get("authorString") or None,
        "abstract": abstract,
        "pub_types": (item.get("pubTypeList") or {}).get("pubType", []),
        "pmid": item.get("pmid"),
        "pmcid": item.get("pmcid"),
        "source_metadata_url": (
            f"https://europepmc.org/article/MED/{item['pmid']}"
            if item.get("pmid")
            else f"https://doi.org/{quote(doi, safe='')}"
        ),
        "metadata_found": True,
        "retraction_status": "unknown",
    }


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def cached_or_fetch(
    client: httpx.Client,
    *,
    topic: str,
    limit: int,
    cache_root: Path,
) -> dict[str, Any]:
    cache_path = cache_root / f"{topic.replace(' ', '-')}.json"
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("limit") == limit and cached.get("status") == "complete":
            return {**cached, "_cache_hit": True}
    query = (
        f'TITLE_ABS:"{topic}" AND HAS_ABSTRACT:y '
        "AND FIRST_PDATE:[2015-01-01 TO 2026-12-31] sort_cited:y"
    )
    error = None
    for attempt in range(4):
        try:
            response = client.get(
                ENDPOINT,
                params={"query": query, "format": "json", "resultType": "core", "pageSize": limit},
            )
            response.raise_for_status()
            results = response.json().get("resultList", {}).get("result", [])
            if not isinstance(results, list):
                raise ValueError("Europe PMC resultList is not a list")
            payload = {
                "topic": topic,
                "query": query,
                "status": "complete",
                "limit": limit,
                "source_hit_count": response.json().get("hitCount"),
                "result_count": len(results),
                "results": results,
                "_cache_hit": False,
            }
            atomic_json(cache_path, payload)
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"Europe PMC query failed for {topic}: {type(error).__name__}")


def expand(*, limit: int = 250, output: Path | None = None) -> dict[str, Any]:
    if not 50 <= limit <= 1000:
        raise ValueError("per-topic limit must be between 50 and 1000")
    original = json.loads((RUNTIME / "extended-corpus.json").read_text(encoding="utf-8"))
    by_work = {record["work_id"]: record for record in original["records"]}
    queries = []
    cache_root = RUNTIME / "expanded-v3"
    with httpx.Client(
        timeout=httpx.Timeout(90, connect=20),
        follow_redirects=True,
        headers={"User-Agent": "citation-tool-research/0.1"},
    ) as client:
        for index, topic in enumerate(TOPICS, 1):
            result = cached_or_fetch(client, topic=topic, limit=limit, cache_root=cache_root)
            before = len(by_work)
            accepted = 0
            for item in result["results"]:
                record = normalize_record(item)
                if record is not None and record["work_id"] not in by_work:
                    by_work[record["work_id"]] = record
                    accepted += 1
            queries.append(
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"results", "_cache_hit"}
                }
            )
            print(
                f"[{index}/{len(TOPICS)}] {topic}: fetched={result['result_count']}, "
                f"new={accepted}, unique={len(by_work)}, delta={len(by_work) - before}",
                flush=True,
            )
            if not result["_cache_hit"]:
                time.sleep(0.15)
    records = sorted(by_work.values(), key=lambda record: record["work_id"])
    if len(records) <= len(original["records"]):
        raise RuntimeError("expanded corpus did not add any records")
    payload = {
        "corpus_id": f"abiotech-with-europepmc-multidomain-v3-n{len(records)}",
        "source": original["source"] + "; v3 Europe PMC multi-domain topic snapshot",
        "retrieval_scope": original["retrieval_scope"],
        "citation_scope": original["citation_scope"],
        "governance_scope": (
            "Core 50 retain their checked governance snapshot. Additional records have unknown "
            "retraction status and are candidate-discovery metadata, not checked citations."
        ),
        "core_work_ids": original["core_work_ids"],
        "source_queries": queries,
        "snapshot_created_at": utc_now_iso(),
        "records": records,
    }
    destination = output or RUNTIME / "large-corpus.json"
    atomic_json(destination, payload)
    manifest = {
        **{key: value for key, value in payload.items() if key != "records"},
        "record_count": len(records),
        "core_count": len(payload["core_work_ids"]),
        "abstracts_available": sum(bool(record["abstract"]) for record in records),
        "records": [
            {key: value for key, value in record.items() if key != "abstract"}
            | {"abstract_characters": len(record["abstract"])}
            for record in records
        ],
    }
    atomic_json(PUBLIC / "large-corpus-manifest.json", manifest)
    return {
        "corpus_id": payload["corpus_id"],
        "records": len(records),
        "new_records": len(records) - len(original["records"]),
        "topics": len(queries),
        "output": str(destination),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-topic", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(expand(limit=args.per_topic, output=args.output), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
