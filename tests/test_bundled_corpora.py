from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest
from tools.bundle_corpora import ALLOWED_LICENSES, public_record, rights_index

from citation_tool.config import (
    BIBLIOGRAPHIC_ONLY,
    BUNDLED_CORPORA,
    BUNDLED_CORPUS_PATH,
    BUNDLED_SAMPLE_PATH,
)
from citation_tool.evidence import build_recommendation
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.runtime import load_metadata_records
from test_online_sources import online_record, snapshot_library, snapshot_record
from test_units import make_claim


def test_published_catalog_matches_manifest_and_carries_per_abstract_attribution():
    manifest = json.loads((BUNDLED_CORPORA / "manifest.json").read_bytes())["libraries"]
    ids = []
    for path, count in ((BUNDLED_CORPUS_PATH, 11_002), (BUNDLED_SAMPLE_PATH, 50)):
        rows = load_metadata_records(path)
        by_id = {row["work_id"]: row for row in rows}
        ids.append(set(by_id))
        assert len(rows) == len(by_id) == count == manifest[path.name]["record_count"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[path.name]["sha256"]
        licensed = Counter(row["abstract_license"] for row in rows if row["abstract"])
        assert licensed == manifest[path.name]["abstract_licenses"]
        assert licensed.total() == manifest[path.name]["abstract_count"]
        for row in rows:
            assert not {"source_pdf", "full_text", "local_text_layer_spotchecked"} & row.keys()
            assert row["source_metadata_url"].startswith(("https://europepmc.org/", "https://doi.org/"))
            if row["abstract"]:
                assert row["abstract_license"] in ALLOWED_LICENSES.values()
                assert row["abstract_license_source"].startswith("https://europepmc.org/articles/PMC")
                assert row["authors"]
            else:
                assert row["evidence_level"] == BIBLIOGRAPHIC_ONLY
                assert "abstract_license" not in row
    assert ids[1] <= ids[0]
    assert manifest[BUNDLED_SAMPLE_PATH.name]["abstract_count"] == 50


@pytest.mark.parametrize(
    "updates, allowed",
    [
        ({}, True),
        ({"isOpenAccess": "N"}, False),
        ({"license": "cc by-nc-nd"}, False),
        ({"license": None}, False),
        ({"abstractText": "Different version."}, False),
        ({"doi": "10.1234/not-same-work"}, False),
        ({"pmcid": "not-the-license-source"}, False),
    ],
)
def test_public_projection_requires_matching_permissive_grant(tmp_path, updates, allowed):
    source = snapshot_record() | {"source_pdf": "private.pdf", "full_text": "never ship"}
    rights = tmp_path / "rights.json"
    receipt = online_record(isOpenAccess="Y", pmcid="PMC100", license="cc by") | updates
    rights.write_text(json.dumps({"results": [receipt]}), encoding="utf-8")
    row = public_record(source, rights_index([rights]))
    assert row["work_id"] == source["work_id"]
    assert row["abstract"] == (source["abstract"] if allowed else "")
    assert ("abstract_license" in row) is allowed
    assert not {"source_pdf", "full_text"} & row.keys()


def test_bibliography_is_searchable_without_abstract_or_citation_evidence(tmp_path):
    row = public_record(snapshot_record(), {})
    corpus = tmp_path / "bibliographic.json"
    corpus.write_text(json.dumps({"records": [row]}), encoding="utf-8")
    adapter = MetadataCorpusAdapter(snapshot_library(), load_metadata_records(corpus))
    claim = make_claim(text="LEAFY floral organ development")
    [candidate] = adapter.search(claim)
    assert candidate.quote == candidate.context == ""
    assert candidate.passage.passage_id.endswith("#bibliography")
    assert not candidate.document_type_eligible
    result = build_recommendation(
        candidate=candidate, tenant_id=claim.tenant_id, claim=claim, query_id=claim.query_id
    )
    assert not result.citable and not result.evidence[0].text_offset_valid
    row.pop("evidence_level")
    corpus.write_text(json.dumps({"records": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="marked bibliography-only"):
        load_metadata_records(corpus)
