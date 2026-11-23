"""Build a frozen real-abstract corpus with near-neighbour distractors."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from citation_tool.entities import utc_now_iso
from citation_tool.publication import canonical_work_id

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data/runtime/quality-corpus"
PUBLIC = ROOT / "benchmark/quality"
ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TOPICS = ("soybean", "floral organ", "nodule", "pangenome", "single-cell", "RNA modification")


def expand() -> None:
    original = json.loads((RUNTIME / "real-corpus.json").read_text(encoding="utf-8"))
    by_work = {record["work_id"]: record for record in original["records"]}
    core_ids = sorted(by_work)
    queries = []
    with httpx.Client(timeout=60) as client:
        for topic in TOPICS:
            query = (
                f'TITLE_ABS:"{topic}" AND HAS_ABSTRACT:y '
                "AND FIRST_PDATE:[2022-01-01 TO 2026-12-31] sort_cited:y"
            )
            cache_path = RUNTIME / "expanded-sources" / f"{topic.replace(' ', '-')}.json"
            if cache_path.is_file():
                result = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                result = None
                for attempt in range(3):
                    try:
                        response = client.get(
                            ENDPOINT,
                            params={
                                "query": query,
                                "format": "json",
                                "resultType": "core",
                                "pageSize": 75,
                            },
                        )
                        response.raise_for_status()
                        result = response.json().get("resultList", {}).get("result", [])
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        if attempt == 2:
                            print(
                                f"metadata query failed: {topic}: {type(exc).__name__}", flush=True
                            )
                        else:
                            time.sleep(2**attempt)
                if result is None:
                    queries.append({"topic": topic, "query": query, "status": "failed"})
                    continue
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                temporary.replace(cache_path)
            queries.append(
                {"topic": topic, "query": query, "status": "complete", "result_count": len(result)}
            )
            for item in result:
                doi = item.get("doi")
                if not isinstance(doi, str) or not doi:
                    continue
                work_id = canonical_work_id({"doi": doi})
                if work_id in by_work:
                    continue
                abstract = re.sub(
                    r"<[^>]+>", "", html.unescape(item.get("abstractText") or "")
                ).strip()
                title = re.sub(r"<[^>]+>", "", html.unescape(item.get("title") or "")).strip()
                if len(abstract) < 300 or not title:
                    continue
                source_id = item.get("pmid") or item.get("id") or work_id
                by_work[work_id] = {
                    "work_id": work_id,
                    "paper_id": f"paper:europepmc:{source_id}",
                    "library_id": "europepmc-near-neighbour",
                    "simulation": False,
                    "evidence_level": "abstract_metadata_only",
                    "title": title,
                    "year": int(item["pubYear"]) if item.get("pubYear", "").isdigit() else None,
                    "doi": doi.casefold(),
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
            time.sleep(0.2)
    records = sorted(by_work.values(), key=lambda record: record["work_id"])
    checked = utc_now_iso()
    payload = {
        "corpus_id": "abiotech-50-with-europepmc-near-neighbours-v1",
        "source": original["source"] + "; additional Europe PMC core near-neighbour queries",
        "retrieval_scope": original["retrieval_scope"],
        "citation_scope": original["citation_scope"],
        "governance_scope": (
            "Core 50 use checked governance snapshot. Additional distractors have unknown "
            "retraction status and cannot be presented as checked full-text citations."
        ),
        "core_work_ids": core_ids,
        "decoy_queries": queries,
        "snapshot_created_at": checked,
        "records": records,
    }
    (RUNTIME / "extended-corpus.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        **{key: value for key, value in payload.items() if key != "records"},
        "record_count": len(records),
        "core_count": len(core_ids),
        "abstracts_available": sum(bool(record["abstract"]) for record in records),
        "records": [
            {key: value for key, value in record.items() if key != "abstract"}
            | {"abstract_characters": len(record["abstract"])}
            for record in records
        ],
    }
    (PUBLIC / "extended-corpus-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({"records": len(records), "core": len(core_ids), "queries": queries}, indent=2)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    expand()
