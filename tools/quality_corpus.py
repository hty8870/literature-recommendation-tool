from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PDF_ROOT = (
    ROOT
    / "data/source/农科院aBiotech期刊智能体项目_50篇测试文献"
    / "农科院aBiotech期刊智能体项目_50篇测试文献"
)
RUNTIME = ROOT / "data/runtime/quality-corpus"
PUBLIC = ROOT / "benchmark/quality"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def prepare() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    source_files = sorted(PDF_ROOT.glob("s42994-*.pdf"))
    if len(source_files) != 50:
        raise ValueError(f"expected 50 source PDFs, found {len(source_files)}")
    response = httpx.get(
        EUROPE_PMC,
        params={
            "query": 'JOURNAL:"aBIOTECH"',
            "format": "json",
            "resultType": "core",
            "pageSize": 500,
        },
        timeout=60,
    )
    response.raise_for_status()
    remote = response.json()["resultList"]["result"]
    by_doi = {item.get("doi", "").casefold(): item for item in remote if item.get("doi")}
    records = []
    for source in source_files:
        doi = f"10.1007/{source.stem}"
        item = by_doi.get(doi, {})
        abstract = item.get("abstractText")
        if not isinstance(abstract, str):
            abstract = ""
        abstract = re.sub(r"<[^>]+>", "", html.unescape(abstract)).strip()
        title = re.sub(r"<[^>]+>", "", html.unescape(item.get("title", ""))).strip()
        records.append(
            {
                "work_id": f"work:{doi}",
                "paper_id": f"paper:abiotech:{source.stem}",
                "library_id": "abiotech-real",
                "simulation": False,
                "evidence_level": "abstract_metadata_only",
                "title": title,
                "year": int(item["pubYear"]) if item.get("pubYear", "").isdigit() else None,
                "doi": doi,
                "authors": item.get("authorString") or None,
                "abstract": abstract,
                "pub_types": (item.get("pubTypeList") or {}).get("pubType", []),
                "pmid": item.get("pmid"),
                "pmcid": item.get("pmcid"),
                "source_pdf": source.name,
                "source_metadata_url": f"https://europepmc.org/article/MED/{item['pmid']}"
                if item.get("pmid")
                else None,
                "metadata_found": bool(item),
                "local_text_layer_spotchecked": source.stem == "s42994-024-00191-3",
            }
        )
    payload = {
        "corpus_id": "abiotech-50-abstracts-v1",
        "source": "Europe PMC core metadata queried by journal aBIOTECH; local PDF filenames",
        "retrieval_scope": "candidate discovery from titles and abstracts only",
        "citation_scope": "not full-text or citation eligibility evidence",
        "records": records,
    }
    (RUNTIME / "real-corpus.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    public_records = [
        {key: value for key, value in record.items() if key != "abstract"}
        | {"abstract_characters": len(record["abstract"])}
        for record in records
    ]
    (PUBLIC / "corpus-manifest.json").write_text(
        json.dumps(
            {
                **{key: value for key, value in payload.items() if key != "records"},
                "record_count": len(records),
                "metadata_found": sum(item["metadata_found"] for item in records),
                "abstracts_available": sum(bool(item["abstract"]) for item in records),
                "full_text_spotchecks": sum(
                    item["local_text_layer_spotchecked"] for item in records
                ),
                "records": public_records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "records": len(records),
                "metadata_found": sum(item["metadata_found"] for item in records),
                "abstracts_available": sum(bool(item["abstract"]) for item in records),
                "missing_dois": [item["doi"] for item in records if not item["metadata_found"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def govern() -> None:
    from citation_tool.entities import utc_now_iso
    from citation_tool.publication import normalize_doi, publication_status, related_dois

    corpus_path = RUNTIME / "real-corpus.json"
    source_path = RUNTIME / "governance-source.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    remote = json.loads(source_path.read_text(encoding="utf-8"))
    by_doi = {normalize_doi(item["doi"]): item for item in remote["records"]}
    checked_at = utc_now_iso()
    for record in corpus["records"]:
        item = by_doi.get(normalize_doi(record["doi"]))
        if item is None:
            raise ValueError(f"missing governance metadata: {record['doi']}")
        relations = {key: [] for key in ("corrects", "corrected_by", "retracted_by")}
        for link in (item.get("corrections") or {}).get("commentCorrection", []):
            match = re.search(r"10\.\d{4,9}/[^\s.]+(?:\.[^\s.]+)*", link.get("reference", ""))
            if not match:
                continue
            kind = link.get("type", "").casefold()
            target = (
                "corrects"
                if kind == "erratum for"
                else "corrected_by"
                if kind == "erratum in"
                else "retracted_by"
                if kind == "retraction in"
                else None
            )
            if target and (doi := normalize_doi(match.group())):
                relations[target].append(doi)
        record["related_dois"] = {key: sorted(set(values)) for key, values in relations.items()}
        record["retraction_status"] = (
            "retracted"
            if str(item.get("retracted") or "").casefold() in {"y", "yes", "true"}
            or publication_status(record) == "retracted"
            else "corrected"
            if relations["corrected_by"]
            else "none"
        )
        record["status_verified_at"] = checked_at
        record["status_source"] = "Europe PMC core commentCorrectionList and publication types"
    corpus["governance_scope"] = (
        "Snapshot-level checks, not a permanent guarantee. "
        "Unknown/unverified status stays unknown; "
        "retraction records must be rechecked before full-text citation or export."
    )
    corpus_path.write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path = PUBLIC / "corpus-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    governance = {
        record["work_id"]: {
            "related_dois": related_dois(record),
            "retraction_status": publication_status(record),
            "status_verified_at": record["status_verified_at"],
            "status_source": record["status_source"],
        }
        for record in corpus["records"]
    }
    for record in manifest["records"]:
        record.update(governance[record["work_id"]])
    manifest["governance_scope"] = corpus["governance_scope"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "records": len(governance),
                "checked_at": checked_at,
                "corrected": sum(
                    item["retraction_status"] == "corrected" for item in governance.values()
                ),
                "retracted": sum(
                    item["retraction_status"] == "retracted" for item in governance.values()
                ),
            },
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "govern"])
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "govern":
        govern()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
