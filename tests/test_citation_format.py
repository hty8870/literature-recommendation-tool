from __future__ import annotations

import json

import pytest

from citation_tool import citation_format
from citation_tool.citation_format import (
    CitationAuthor,
    CitationFormatError,
    CitationMetadata,
    format_citation,
    serialize_bibtex,
    serialize_reference,
    serialize_ris,
)

METADATA = CitationMetadata(
    title=(
        "Growth Performance and Fitness Analysis of Backcross Progenies of "
        "Herbicide-Tolerant Transgenic Soybean and Wild Soybean"
    ),
    authors=[
        CitationAuthor(family="Zhang", given="Li"),
        CitationAuthor(family="Wang", given="Xiaojing"),
    ],
    year=2026,
    month=9,
    day=7,
    container_title="Plants",
    volume="15",
    issue="17",
    page="2730",
    doi="10.3390/plants15172730",
    pmid="42739497",
    pmcid="PMC13568360",
    language="eng",
    license="cc by",
)


def test_official_style_assets_are_present_and_hashed():
    apa = citation_format._style_info("apa")
    vancouver = citation_format._style_info("vancouver")
    assert apa.style_id == "http://www.zotero.org/styles/apa"
    assert apa.citation_format == "author-date"
    assert apa.sha256 == ("3917def5fc951b104d64216c3373096042368b9704ba269ca83c14ebaba889fc")
    assert vancouver.style_id == "http://www.zotero.org/styles/nlm-citation-sequence"
    assert vancouver.citation_format == "numeric"
    assert vancouver.sha256 == ("3bcebfe5ac07cba3d17716e77e6ded213435fba77ceb28a55e8e39f1eb9d2dfd")
    notice = (citation_format.CSL_ASSET_DIR / "NOTICE.md").read_text(encoding="utf-8")
    assert "Creative Commons Attribution-ShareAlike 3.0" in notice


def test_missing_citeproc_dependency_is_explicit(monkeypatch):
    def missing():
        raise CitationFormatError(
            "CITATION_FORMAT_DEPENDENCY_MISSING",
            503,
            "missing citeproc-py",
        )

    monkeypatch.setattr(citation_format, "_load_citeproc", missing)
    with pytest.raises(CitationFormatError) as caught:
        format_citation(METADATA, "apa")
    assert caught.value.code == "CITATION_FORMAT_DEPENDENCY_MISSING"
    assert caught.value.status == 503


def test_missing_volume_is_a_field_level_error():
    metadata = METADATA.model_copy(update={"volume": None})
    with pytest.raises(CitationFormatError) as caught:
        format_citation(metadata, "apa")
    assert caught.value.code == "CITATION_METADATA_INCOMPLETE"
    assert "volume" in caught.value.details["missing_fields"]


def test_invalid_doi_is_not_guessed():
    metadata = METADATA.model_copy(update={"doi": "not-a-doi"})
    with pytest.raises(CitationFormatError) as caught:
        format_citation(metadata, "vancouver")
    assert caught.value.code == "CITATION_METADATA_CONFLICT"
    assert caught.value.details["field"] == "doi"


def test_ris_serialization_is_deterministic():
    first = serialize_ris(METADATA)
    second = serialize_ris(METADATA)
    assert first == second
    assert first.startswith("TY  - JOUR\n")
    assert "AU  - Zhang, Li\n" in first
    assert "AU  - Wang, Xiaojing\n" in first
    assert f"TI  - {METADATA.title}\n" in first
    assert "JO  - Plants\n" in first
    assert "VL  - 15\n" in first
    assert "IS  - 17\n" in first
    assert "PY  - 2026\n" in first
    assert "DO  - 10.3390/plants15172730\n" in first
    assert "SP  - 2730\n" in first
    assert first.endswith("ER  -\n")


def test_bibtex_serialization_is_deterministic_and_escaped():
    first = serialize_bibtex(METADATA)
    second = serialize_bibtex(METADATA)
    assert first == second
    assert first.startswith("@article{zhang2026growth,\n")
    assert "  author = {Zhang, Li and Wang, Xiaojing},\n" in first
    assert "  doi = {10.3390/plants15172730}\n" in first
    escaped = serialize_bibtex(METADATA.model_copy(update={"title": "A & B_100% {test}"}))
    assert r"A \& B\_100\% \{test\}" in escaped


def test_csl_json_is_machine_readable():
    document = json.loads(serialize_reference(METADATA, "csl-json"))
    assert document["type"] == "article-journal"
    assert document["DOI"] == "10.3390/plants15172730"
    assert document["author"][0] == {"family": "Zhang", "given": "Li"}
    assert document["issued"]["date-parts"] == [[2026, 9, 7]]


def test_apa_and_vancouver_render_with_official_styles():
    pytest.importorskip("citeproc")
    apa = format_citation(METADATA, "apa")
    assert apa.style_id == "http://www.zotero.org/styles/apa"
    assert apa.numbering_required is False
    assert apa.in_text == "(Zhang & Wang, 2026)"
    assert "Zhang, L., & Wang, X. (2026)." in apa.bibliography
    assert "Plants, 15(17), 2730." in apa.bibliography
    assert "https://doi.org/10.3390/plants15172730" in apa.bibliography

    vancouver = format_citation(METADATA, "vancouver")
    assert vancouver.style_id == "http://www.zotero.org/styles/nlm-citation-sequence"
    assert vancouver.numbering_required is True
    assert vancouver.in_text is None
    assert vancouver.bibliography.startswith("Zhang L, Wang X.")
    assert not vancouver.bibliography.startswith("1.")
    assert "Plants. 2026 Sep 7;15(17):2730." in vancouver.bibliography
    assert "doi: 10.3390/plants15172730" in vancouver.bibliography
    assert vancouver.postprocess == "numeric-spacing-v1"


def test_literal_author_can_be_formatted():
    metadata = METADATA.model_copy(
        update={"authors": [CitationAuthor(literal="Soybean Research Consortium")]}
    )
    assert "Soybean Research Consortium" in serialize_ris(metadata)
