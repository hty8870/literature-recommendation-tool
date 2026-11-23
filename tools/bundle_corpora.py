"""Publish exact bibliographic membership without copying unverified abstracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tools.quality_corpus_expand_v3 import ENDPOINT, RUNTIME, atomic_json, clean_text

from citation_tool.config import (
    BIBLIOGRAPHIC_ONLY,
    BUNDLED_CORPORA,
    BUNDLED_CORPUS_PATH,
    BUNDLED_SAMPLE_PATH,
)
from citation_tool.publication import canonical_work_id, normalize_doi, related_dois
from citation_tool.runtime import load_metadata_records

ALLOWED_LICENSES = {"cc by": "CC BY", "cc0": "CC0"}
TERMS_URL = "https://europepmc.org/Copyright"


@dataclass(frozen=True, order=True, slots=True)
class Grant:
    license: str
    source_url: str


def rights_index(paths: Iterable[Path]) -> dict[tuple[str, str, str], Grant]:
    grants: dict[tuple[str, str, str], Grant] = {}
    for path in paths:
        payload = json.loads(path.read_bytes())
        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(f"expected saved Europe PMC core results in {path}")
        for row in rows:
            if not isinstance(row, dict) or row.get("isOpenAccess") != "Y":
                continue
            license_name = ALLOWED_LICENSES.get(str(row.get("license")).strip().casefold())
            pmcid = str(row.get("pmcid") or "")
            if not license_name or not re.fullmatch(r"PMC[0-9]+", pmcid):
                continue
            doi = normalize_doi(row.get("doi"))
            title, abstract = clean_text(row.get("title")), clean_text(row.get("abstractText"))
            if not doi or not title or not abstract:
                continue
            key = doi, title, abstract
            grant = Grant(license_name, f"https://europepmc.org/articles/{pmcid}")
            if key not in grants or grant < grants[key]:
                grants[key] = grant
    return grants


def public_record(
    record: dict, grants: Mapping[tuple[str, str, str], Grant]
) -> dict:
    doi = normalize_doi(record["doi"])
    if not doi or canonical_work_id({"doi": doi}) != record["work_id"]:
        raise ValueError("snapshot record DOI and work ID disagree")
    pmid = record.get("pmid")
    metadata_url = (
        f"https://europepmc.org/article/MED/{pmid}"
        if pmid and re.fullmatch(r"[0-9]+", str(pmid))
        else f"https://doi.org/{quote(doi, safe='/')}"
    )
    authors = record.get("authors")
    grant = grants.get((doi, clean_text(record["title"]), record["abstract"]))
    if not isinstance(authors, str) or not authors.strip():
        grant = None
    output = {
        "work_id": record["work_id"],
        "paper_id": record["paper_id"],
        "simulation": False,
        "title": record["title"],
        "year": record.get("year"),
        "doi": doi,
        "authors": authors,
        "pub_types": record.get("pub_types", []),
        "pmid": record.get("pmid"),
        "pmcid": record.get("pmcid"),
        "source_metadata_url": metadata_url,
        "retraction_status": record.get("retraction_status", "unknown"),
        "related_dois": related_dois(record),
        "status_verified_at": record.get("status_verified_at"),
        "status_source": record.get("status_source"),
        "evidence_level": "abstract_metadata_only" if grant else BIBLIOGRAPHIC_ONLY,
        "abstract": record["abstract"] if grant else "",
    }
    if grant:
        output.update(abstract_license=grant.license, abstract_license_source=grant.source_url)
    return output


def encode_snapshot(records: list[dict]) -> bytes:
    body = ",\n".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) for row in records)
    return ('{"records":[\n' + body + '\n]}\n').encode("utf-8")


def build_bundle(
    source_paths: tuple[Path, Path], rights_paths: list[Path], output: Path
) -> dict:
    inputs = [load_metadata_records(path) for path in source_paths]
    all_ids = [{record["work_id"] for record in records} for records in inputs]
    if any(len(ids) != len(records) for ids, records in zip(all_ids, inputs, strict=True)):
        raise ValueError("duplicate work IDs in source snapshot")
    if not all_ids[1] <= all_ids[0]:
        raise ValueError("the multidisciplinary snapshot must preserve the aBiotech collection")
    grants = rights_index(rights_paths)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 1,
        "source": ENDPOINT,
        "copyright_terms": TERMS_URL,
        "abstract_policy": (
            "Only exact snapshot abstracts matched to Europe PMC Open Access core results "
            "with a CC BY or CC0 grant and the article license statement link. "
            "Abstracts retain original attribution and wording after markup normalization. "
            "Other records carry bibliographic facts and links only. No article full text."
        ),
        "libraries": {},
    }
    names = (BUNDLED_CORPUS_PATH.name, BUNDLED_SAMPLE_PATH.name)
    bundles = []
    for name, records, original in zip(names, inputs, source_paths, strict=True):
        rows = [public_record(record, grants) for record in records]
        data = encode_snapshot(rows)
        licenses = Counter(record["abstract_license"] for record in rows if record["abstract"])
        manifest["libraries"][name] = {
            "record_count": len(rows),
            "abstract_count": licenses.total(),
            "abstract_licenses": dict(sorted(licenses.items())),
            "sha256": hashlib.sha256(data).hexdigest(),
            "original_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        }
        bundles.append((output / name, data))
    for path, data in bundles:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    atomic_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--multidisciplinary", type=Path, default=RUNTIME / "large-corpus.json")
    parser.add_argument("--sample", type=Path, default=RUNTIME / "real-corpus.json")
    parser.add_argument("--rights", type=Path, nargs="+", required=True, help="core JSON exports")
    parser.add_argument("--output", type=Path, default=BUNDLED_CORPORA)
    args = parser.parse_args()
    manifest = build_bundle((args.multidisciplinary, args.sample), args.rights, args.output)
    print(json.dumps(manifest["libraries"], indent=2))


if __name__ == "__main__":
    main()
