from __future__ import annotations

import hashlib
import json
import re
import textwrap

import pytest

from citation_tool.citation_sources import (
    MAX_FULLTEXT_BYTES,
    CitationIdentity,
    CitationSourceError,
    EuropePmcSourceResolver,
    HttpResponse,
    HttpsTransport,
)

DOI = "10.3390/plants15172730"
PMID = "42739497"
PMCID = "PMC13568360"
TITLE = (
    "Growth Performance and Fitness Analysis of Backcross Progenies of "
    "Herbicide-Tolerant Transgenic Soybean and Wild Soybean"
)

JATS_XML = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <article xml:lang="en" article-type="research-article">
      <front>
        <journal-meta>
          <journal-title-group><journal-title>Plants</journal-title></journal-title-group>
        </journal-meta>
        <article-meta>
          <article-id pub-id-type="pmcid">PMC13568360</article-id>
          <article-id pub-id-type="pmid">42739497</article-id>
          <article-id pub-id-type="doi">10.3390/plants15172730</article-id>
          <title-group><article-title>Growth Performance and Fitness Analysis of
          Backcross Progenies of Herbicide-Tolerant Transgenic Soybean and Wild
          Soybean</article-title></title-group>
          <contrib-group>
            <contrib contrib-type="author"><name><surname>Zhang</surname>
              <given-names>Li</given-names></name></contrib>
            <contrib contrib-type="author"><name><surname>Wang</surname>
              <given-names>Xiaojing</given-names></name></contrib>
          </contrib-group>
          <contrib-group content-type="editor">
            <contrib><name><surname>Albertini</surname>
              <given-names>Emidio</given-names></name><role>Academic Editor</role></contrib>
          </contrib-group>
          <pub-date pub-type="ppub"><day>7</day><month>9</month><year>2026</year></pub-date>
          <volume>15</volume><issue>17</issue><fpage>2730</fpage>
          <permissions><license license-type="open-access">This article is an open
          access article distributed under the terms and conditions of the Creative
          Commons Attribution (CC BY) license.</license></permissions>
        </article-meta>
      </front>
      <body>
        <sec id="sec-results">
          <title>Results</title>
          <p id="p-1">Herbicide resistance and fitness were measured in backcross
          progenies of transgenic and wild soybean under greenhouse conditions.
          The BC1F1 plants showed stable expression of the exogenous EPSPS protein
          and measurable differences in seed dormancy and growth across sampling
          stages.</p>
          <p id="p-2">Seed dormancy and germination differed between backcross
          progenies and wild soybean. These fitness-related traits are relevant to
          ecological risk assessment of transgenic soybean gene flow.</p>
        </sec>
        <sec id="sec-methods">
          <title>Materials and Methods</title>
          <p id="p-3">Two parents, GM soybean GTS40-3-2 and Jiangpu wild soybean,
          were cultivated under controlled greenhouse conditions. Herbicide
          resistance was measured with a greenhouse spray assay.</p>
        </sec>
        <sec id="sec-references"><title>References</title>
          <p id="p-ref">1. A reference paragraph that must never be used as evidence.</p>
        </sec>
      </body>
    </article>
    """
)


def search_record(**overrides):
    record = {
        "id": PMID,
        "source": "MED",
        "pmid": PMID,
        "pmcid": PMCID,
        "doi": DOI,
        "title": TITLE + ".",
        "authorString": "Zhang L, Wang X.",
        "authorList": {
            "author": [
                {"lastName": "Zhang", "firstName": "Li"},
                {"lastName": "Wang", "firstName": "Xiaojing"},
            ]
        },
        "journalInfo": {
            "volume": "15",
            "issue": "17",
            "journal": {"title": "Plants"},
        },
        "pubYear": "2026",
        "pageInfo": "2730",
        "isOpenAccess": "Y",
        "inEPMC": "Y",
        "license": "cc by",
        "pubTypeList": {"pubType": ["research-article", "Journal Article"]},
    }
    record.update(overrides)
    return record


def identity(**overrides):
    values = {
        "title": TITLE,
        "authors": "Zhang L, Wang X",
        "year": 2026,
        "doi": DOI,
        "pmid": PMID,
        "pmcid": PMCID,
    }
    values.update(overrides)
    return CitationIdentity(**values)


class FakeTransport:
    def __init__(self, record=None, xml=JATS_XML, *, search_status=200, xml_status=200):
        self.record = search_record() if record is None else record
        self.xml = xml
        self.search_status = search_status
        self.xml_status = xml_status
        self.urls: list[str] = []

    def get(self, url: str, *, accept: str, timeout_s: float, max_bytes: int):
        self.urls.append(url)
        if "/search?" in url:
            if self.search_status != 200:
                return HttpResponse(self.search_status, {}, b"")
            payload = {"version": "6.9", "resultList": {"result": [self.record]}}
            return HttpResponse(200, {}, json.dumps(payload).encode("utf-8"))
        if "fullTextXML" in url:
            if self.xml_status != 200:
                return HttpResponse(self.xml_status, {}, b"")
            body = self.xml.encode("utf-8")
            assert len(body) < max_bytes
            return HttpResponse(200, {}, body)
        raise AssertionError(f"unexpected URL: {url}")


def resolver(transport):
    return EuropePmcSourceResolver(transport=transport, timeout_ms=5000, max_evidence=3)


def test_resolve_returns_exact_fulltext_quotes_and_metadata():
    transport = FakeTransport()
    bundle = resolver(transport).resolve(
        identity(), "herbicide resistance and fitness in backcross soybean"
    )
    assert bundle.metadata.title == TITLE
    assert bundle.metadata.container_title == "Plants"
    assert bundle.metadata.volume == "15"
    assert bundle.metadata.issue == "17"
    assert bundle.metadata.page == "2730"
    assert bundle.metadata.doi == DOI
    assert bundle.metadata.pmcid == PMCID
    assert [author.family for author in bundle.metadata.authors] == ["Zhang", "Wang"]
    assert bundle.provenance.source_level == "fulltext_xml"
    assert bundle.provenance.fulltext_sha256
    assert all(evidence.source_level == "fulltext_xml" for evidence in bundle.evidence)
    assert bundle.evidence
    for evidence in bundle.evidence:
        assert evidence.paragraph_text[evidence.start_offset : evidence.end_offset] == (
            evidence.quote
        )
        assert hashlib.sha256(evidence.quote.encode("utf-8")).hexdigest() == (evidence.quote_sha256)
        assert hashlib.sha256(evidence.paragraph_text.encode("utf-8")).hexdigest() == (
            evidence.paragraph_sha256
        )
        assert evidence.source_url == f"https://europepmc.org/article/PMC/{PMCID}"
    assert not any(
        "reference" in " ".join(item.section_path).casefold() for item in bundle.evidence
    )
    assert all("fullTextXML" in url for url in transport.urls if "/search?" not in url)


def test_title_and_doi_mismatch_is_rejected():
    transport = FakeTransport(record=search_record(doi="10.3390/other"))
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(identity(), "herbicide resistance soybean")
    assert caught.value.code == "CITATION_IDENTITY_UNRESOLVED"


def test_title_mismatch_is_rejected():
    transport = FakeTransport(record=search_record(title="A completely different paper"))
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(
            identity(doi=None, pmid=None, pmcid=None), "herbicide resistance soybean"
        )
    assert caught.value.code == "CITATION_IDENTITY_UNRESOLVED"


def test_retracted_paper_is_rejected_before_fulltext():
    transport = FakeTransport(record=search_record(retracted="Y"))
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(identity(), "herbicide resistance soybean")
    assert caught.value.code == "CITATION_RETRACTED"


def test_candidate_related_retraction_is_rejected():
    transport = FakeTransport()
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(
            identity(related_dois={"retracted_by": ["10.3390/retraction"]}),
            "herbicide resistance soybean",
        )
    assert caught.value.code == "CITATION_RETRACTED"


def test_corrected_paper_is_rejected():
    record = search_record(
        commentCorrectionList={"commentCorrection": [{"type": "Erratum in", "reference": "10.1/x"}]}
    )
    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport(record=record)).resolve(identity(), "herbicide resistance")
    assert caught.value.code == "CITATION_CORRECTED"


def test_non_open_access_paper_is_rejected():
    transport = FakeTransport(record=search_record(isOpenAccess="N"))
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(identity(), "herbicide resistance")
    assert caught.value.code == "CITATION_SOURCE_NOT_OPEN"


def test_license_conflict_is_rejected():
    xml = re.sub(
        r"<license[^>]*>.*?</license>",
        '<license license-type="all-rights-reserved">All rights reserved.</license>',
        JATS_XML,
        flags=re.DOTALL,
    )
    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport(xml=xml)).resolve(identity(), "herbicide resistance")
    assert caught.value.code == "CITATION_LICENSE_CONFLICT"


def test_doctype_and_entity_xml_is_rejected():
    xml = '<!DOCTYPE article [<!ENTITY x "boom">]>' + JATS_XML
    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport(xml=xml)).resolve(identity(), "herbicide resistance")
    assert caught.value.code == "CITATION_SOURCE_UNSAFE_XML"


def test_redirect_is_not_followed():
    class RedirectTransport(FakeTransport):
        def get(self, url: str, *, accept: str, timeout_s: float, max_bytes: int):
            raise CitationSourceError("CITATION_SOURCE_REDIRECT_REFUSED", 502, "redirect refused")

    with pytest.raises(CitationSourceError) as caught:
        resolver(RedirectTransport()).resolve(identity(), "herbicide resistance")
    assert caught.value.code == "CITATION_SOURCE_REDIRECT_REFUSED"


def test_only_official_europepmc_host_is_allowed():
    with pytest.raises(CitationSourceError) as caught:
        HttpsTransport().get(
            "https://evil.example/europepmc/webservices/rest/search",
            accept="application/json",
            timeout_s=1,
            max_bytes=MAX_FULLTEXT_BYTES,
        )
    assert caught.value.code == "CITATION_SOURCE_URL_INVALID"


def test_insufficient_paragraph_relevance_is_rejected():
    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport()).resolve(identity(), "quantum entanglement in neutrino astronomy")
    assert caught.value.code == "CITATION_INSUFFICIENT_EVIDENCE"


def test_review_article_is_marked_as_secondary():
    record = search_record(pubTypeList={"pubType": ["review"]})
    bundle = resolver(FakeTransport(record=record)).resolve(
        identity(), "herbicide resistance and fitness in backcross soybean"
    )
    assert bundle.evidence
    assert all(item.evidence_role == "review_secondary" for item in bundle.evidence)


def test_missing_pmcid_is_rejected():
    transport = FakeTransport(record=search_record(pmcid=None))
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(identity(pmcid=None), "herbicide resistance")
    assert caught.value.code == "CITATION_FULLTEXT_UNAVAILABLE"


def _publication_xml(dates: str, pages: str) -> str:
    xml = re.sub(r"<pub-date\b[^>]*>.*?</pub-date>", dates, JATS_XML, count=1, flags=re.DOTALL)
    return xml.replace(
        "<volume>15</volume><issue>17</issue><fpage>2730</fpage>",
        f"<volume>6</volume><issue>1</issue>{pages}",
    )


def _issue_record(
    *,
    pub_year: int | None = 2025,
    page_info: str | None = "12-21",
    journal_fields: dict | None = None,
    **overrides,
) -> dict:
    journal_info = {
        "volume": "6",
        "issue": "1",
        "journal": {"title": "Plants"},
        **(journal_fields or {}),
    }
    return search_record(
        pubYear=pub_year,
        pageInfo=page_info,
        journalInfo=journal_info,
        **overrides,
    )


def test_print_date_precedes_earlier_epub_and_api_completes_same_start_page_range():
    """FND-059: Prefer sourced issue metadata without erasing online publication history."""
    xml = _publication_xml(
        '<pub-date pub-type="epub"><day>22</day><month>12</month><year>2024</year></pub-date>'
        '<pub-date pub-type="ppub"><day>1</day><month>3</month><year>2025</year></pub-date>',
        "<fpage>12</fpage>",
    )
    record = _issue_record(
        journal_fields={
            "yearOfPublication": 2025,
            "monthOfPublication": 3,
            "printPublicationDate": "2025-03-01",
            "dateOfPublication": "2025 Mar",
        },
        firstPublicationDate="2024-12-22",
        electronicPublicationDate="2024-12-22",
    )

    bundle = resolver(FakeTransport(record=record, xml=xml)).resolve(
        identity(year=2025), "herbicide resistance and fitness in backcross soybean"
    )

    assert (bundle.metadata.year, bundle.metadata.month, bundle.metadata.day) == (2025, 3, 1)
    assert bundle.metadata.volume == "6"
    assert bundle.metadata.issue == "1"
    assert bundle.metadata.page == "12-21"
    assert "online_publication_date_differs_from_issue_date" in (
        bundle.provenance.metadata_warnings
    )


def test_different_online_dates_warn_without_rejecting_formal_issue_date():
    xml = _publication_xml(
        '<pub-date pub-type="epub"><day>22</day><month>12</month><year>2024</year></pub-date>'
        '<pub-date pub-type="ppub"><day>1</day><month>3</month><year>2025</year></pub-date>',
        "<fpage>12</fpage>",
    )
    record = _issue_record(
        journal_fields={"yearOfPublication": 2025, "printPublicationDate": "2025-03-01"},
        firstPublicationDate="2024-12-20",
        electronicPublicationDate="2024-12-22",
    )

    bundle = resolver(FakeTransport(record=record, xml=xml)).resolve(
        identity(year=2025), "herbicide resistance and fitness in backcross soybean"
    )

    assert (bundle.metadata.year, bundle.metadata.month, bundle.metadata.day) == (2025, 3, 1)
    assert "online_publication_date_sources_differ" in bundle.provenance.metadata_warnings


def test_untyped_jats_online_date_does_not_override_api_issue_date():
    xml = _publication_xml(
        "<pub-date><day>22</day><month>12</month><year>2024</year></pub-date>",
        "<fpage>12</fpage>",
    )
    record = _issue_record(
        journal_fields={
            "yearOfPublication": 2025,
            "monthOfPublication": 3,
            "printPublicationDate": "2025-03-01",
            "dateOfPublication": "2025 Mar",
        },
        firstPublicationDate="2024-12-22",
        electronicPublicationDate="2024-12-22",
    )

    bundle = resolver(FakeTransport(record=record, xml=xml)).resolve(
        identity(year=2025), "herbicide resistance and fitness in backcross soybean"
    )

    assert (bundle.metadata.year, bundle.metadata.month, bundle.metadata.day) == (2025, 3, 1)
    assert "online_publication_date_differs_from_issue_date" in (
        bundle.provenance.metadata_warnings
    )


def test_api_year_precedence_warns_when_untyped_jats_date_disagrees():
    xml = _publication_xml(
        "<pub-date><day>22</day><month>12</month><year>2024</year></pub-date>",
        "<fpage>12</fpage>",
    )
    record = _issue_record(pub_year=2025)

    bundle = resolver(FakeTransport(record=record, xml=xml)).resolve(
        identity(year=2025), "herbicide resistance and fitness in backcross soybean"
    )

    assert bundle.metadata.year == 2025
    assert "untyped_publication_date_differs_from_selected_date" in (
        bundle.provenance.metadata_warnings
    )


def test_online_date_is_used_and_marked_when_no_issue_date_exists():
    xml = _publication_xml("", "<fpage>12</fpage>")
    record = _issue_record(
        pub_year=None,
        journal_fields={},
        firstPublicationDate="2024-12-22",
        electronicPublicationDate="2024-12-22",
    )

    bundle = resolver(FakeTransport(record=record, xml=xml)).resolve(
        identity(year=2025), "herbicide resistance and fitness in backcross soybean"
    )

    assert (bundle.metadata.year, bundle.metadata.month, bundle.metadata.day) == (2024, 12, 22)
    assert "publication_date_online_only" in bundle.provenance.metadata_warnings


def test_conflicting_formal_publication_dates_are_rejected():
    xml = _publication_xml(
        '<pub-date pub-type="ppub"><day>1</day><month>3</month><year>2025</year></pub-date>',
        "<fpage>12</fpage><lpage>21</lpage>",
    )
    record = _issue_record(
        pub_year=2024,
        journal_fields={
            "yearOfPublication": 2024,
            "monthOfPublication": 3,
            "printPublicationDate": "2024-03-01",
            "dateOfPublication": "2024 Mar",
        },
    )

    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport(record=record, xml=xml)).resolve(
            identity(year=2025), "herbicide resistance and fitness in backcross soybean"
        )

    assert caught.value.code == "CITATION_METADATA_CONFLICT"
    assert caught.value.details["field"] == "publication_date"


@pytest.mark.parametrize("page_info", ["12-25", "13-21"])
def test_conflicting_fulltext_and_api_page_ranges_are_rejected(page_info):
    xml = _publication_xml(
        '<pub-date pub-type="ppub"><day>1</day><month>3</month><year>2025</year></pub-date>',
        "<fpage>12</fpage><lpage>21</lpage>",
    )
    record = _issue_record(page_info=page_info)

    with pytest.raises(CitationSourceError) as caught:
        resolver(FakeTransport(record=record, xml=xml)).resolve(
            identity(year=2025), "herbicide resistance and fitness in backcross soybean"
        )

    assert caught.value.code == "CITATION_METADATA_CONFLICT"
    assert caught.value.details["field"] == "page"
