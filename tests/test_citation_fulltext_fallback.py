from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

import pytest

from citation_tool.citation_format import copy_formats, serialize_bibtex, serialize_ris
from citation_tool.citation_research_square import LICENSE_URL, research_square_url
from citation_tool.citation_sources import CitationSourceError, HttpResponse, HttpsTransport
from test_citation_sources import (
    DOI,
    JATS_XML,
    PMCID,
    FakeTransport,
    identity,
    resolver,
    search_record,
)

PREPRINT_DOI = "10.21203/rs.3.rs-6275143/v1"
PREPRINT_URL = "https://www.researchsquare.com/article/rs-6275143/v1"
CLAIM = "herbicide resistance and fitness in backcross soybean under greenhouse conditions"
BODY = (
    "Herbicide resistance and fitness were measured in backcross soybean under greenhouse "
    "conditions. Natural comparisons were limited to this contained experiment."
)


def preprint_html(**changes):
    record = {
        "identity": "rs-6275143",
        "doi": PREPRINT_DOI,
        "published": True,
        "isWithdrawnOrRetracted": False,
        "manuscriptTitle": identity().title,
        "nonDraftVersions": [
            {"code": 1, "doi": PREPRINT_DOI, "date": "2026-04-24", "status": "published"}
        ],
        "license": {"name": "CC BY 4.0", "url": LICENSE_URL},
        "authors": [{"firstName": "Li", "lastName": "Zhang", "email": "ignored@example.invalid"}],
        "articleType": "Research Article",
        "fulltext": [
            {"header": "Key message", "content": f"<p>{BODY} Excluded page summary.</p>"},
            {
                "header": "Results",
                "content": (
                    f"<div><h2>Greenhouse</h2><p>{BODY}</p>"
                    f"<script>bad text</script><table><tr><td><p>{BODY} table</p></td></tr></table>"
                    f"<h2>References</h2><p>{BODY} citation only</p></div>"
                ),
            },
        ],
    }
    record.update(changes)
    payload = {
        "query": {"identity": "rs-6275143", "version": ["v1"]},
        "props": {"pageProps": {"version": "v1", "initialData": record}},
    }
    safe_json = json.dumps(payload).replace("<", r"\u003c")
    return f'<script id="__NEXT_DATA__" type="application/json">{safe_json}</script>'.encode()


class PreprintTransport(FakeTransport):
    def __init__(self, html=None, **record_changes):
        source = {"doi": PREPRINT_DOI, "pmcid": None, "pmid": None, "source": "PPR"}
        source.update(record_changes)
        super().__init__(record=search_record(**source))
        self.html = preprint_html() if html is None else html

    def get(self, url, **options):
        if urlparse(url).hostname == "www.researchsquare.com":
            assert url == PREPRINT_URL
            self.urls.append(url)
            return HttpResponse(200, {"content-type": "text/html; charset=utf-8"}, self.html)
        return super().get(url, **options)


def preprint_identity(**changes):
    return identity(**{"doi": PREPRINT_DOI, "pmid": None, "pmcid": None, **changes})


def test_pmc_missing_identifier_can_read_exact_open_preprint_and_cite_its_version():
    transport = PreprintTransport()
    bundle = resolver(transport).resolve(preprint_identity(), CLAIM)
    assert transport.urls[-1] == PREPRINT_URL
    assert bundle.provenance.fulltext_sha256 == hashlib.sha256(transport.html).hexdigest()
    assert bundle.provenance.xml_url is None and bundle.provenance.source_level == "fulltext_html"
    assert bundle.metadata.doi == PREPRINT_DOI and bundle.metadata.item_type == "article"
    assert len(bundle.evidence) == 1
    fragment = bundle.evidence[0]
    assert fragment.paragraph_text == BODY and fragment.paragraph_id is None
    assert (
        fragment.section_path == ["Results", "Greenhouse"] and fragment.source_url == PREPRINT_URL
    )
    assert fragment.paragraph_text[fragment.start_offset : fragment.end_offset] == fragment.quote
    assert hashlib.sha256(fragment.quote.encode()).hexdigest() == fragment.quote_sha256
    assert serialize_ris(bundle.metadata).startswith("TY  - UNPB\n")
    assert serialize_bibtex(bundle.metadata).startswith("@unpublished{")
    formats = copy_formats(bundle.metadata)
    assert all(item.get("text") for item in formats)
    for item in formats[:2]:
        assert "[Preprint]" in item["text"]
        assert item["text"].count("Research Square") == 1


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"doi": DOI}, "CITATION_IDENTITY_MISMATCH"),
        ({"manuscriptTitle": "Not the same article"}, "CITATION_IDENTITY_MISMATCH"),
        (
            {"license": {"name": "CC BY 4.0", "url": "https://other.invalid/"}},
            "CITATION_LICENSE_UNCLEAR",
        ),
        ({"isWithdrawnOrRetracted": True}, "CITATION_RETRACTED"),
        ({"published": False}, "CITATION_FULLTEXT_UNAVAILABLE"),
        ({"nonDraftVersions": [{"code": 2, "doi": PREPRINT_DOI}]}, "CITATION_IDENTITY_MISMATCH"),
        (
            {"fulltext": [{"header": "Abstract", "content": f"<p>{BODY}</p>"}]},
            "CITATION_FULLTEXT_UNAVAILABLE",
        ),
    ],
)
def test_preprint_fails_closed(changes, error):
    with pytest.raises(CitationSourceError) as caught:
        resolver(PreprintTransport(preprint_html(**changes))).resolve(preprint_identity(), CLAIM)
    assert caught.value.code == error and not caught.value.retryable


def test_exact_open_preprint_is_independent_of_transient_europe_pmc_lookup(monkeypatch):
    monkeypatch.setattr("citation_tool.citation_sources.time.sleep", lambda _: None)
    source = PreprintTransport()
    source.search_status = 503
    result = resolver(source).resolve(preprint_identity(), CLAIM)
    assert len(source.urls) == 4 and source.urls[-1] == PREPRINT_URL
    assert result.metadata.doi == PREPRINT_DOI and result.evidence


def test_known_retraction_and_access_denial_do_not_enable_source_fallback():
    source = PreprintTransport()
    source.search_status = 503
    with pytest.raises(CitationSourceError) as caught:
        resolver(source).resolve(preprint_identity(publication_status="retracted"), CLAIM)
    assert caught.value.code == "CITATION_RETRACTED" and not source.urls
    source.search_status = 403
    with pytest.raises(CitationSourceError) as caught:
        resolver(source).resolve(preprint_identity(), CLAIM)
    assert not caught.value.retryable and PREPRINT_URL not in source.urls


def test_other_doi_never_uses_related_preprint():
    transport = PreprintTransport(doi=DOI)
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(
            identity(pmcid=None, pmid=None, source_uris=[PREPRINT_URL]), CLAIM
        )
    assert caught.value.code == "CITATION_FULLTEXT_UNAVAILABLE"
    assert len(transport.urls) == 1


@pytest.mark.parametrize(
    "url",
    [
        PREPRINT_URL + "/extra",
        PREPRINT_URL + "?to=other",
        "https://www.researchsquare.com/article/rs-6275143/latest",
        "https://www.researchsquare.com.evil.invalid/article/rs-6275143/v1",
    ],
)
def test_only_exact_versioned_preprint_url_is_allowed(url):
    with pytest.raises(CitationSourceError) as caught:
        HttpsTransport().get(url, accept="text/html", timeout_s=1, max_bytes=1000)
    assert caught.value.code == "CITATION_SOURCE_URL_INVALID"
    assert research_square_url(PREPRINT_DOI.removesuffix("/v1")) is None


class AvailabilityTransport(FakeTransport):
    def __init__(self, *, failures, headers=None):
        super().__init__()
        self.remaining = failures.copy()
        self.headers = headers or {}
        self.oai_called = False

    def get(self, url, **options):
        if urlparse(url).hostname == "pmc.ncbi.nlm.nih.gov":
            self.oai_called = True
            identifier = f"oai:pubmedcentral.nih.gov:{PMCID.removeprefix('PMC')}"
            article = JATS_XML.split("?>", 1)[1]
            xml = (
                '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                f"<GetRecord><record><header><identifier>{identifier}</identifier></header>"
                f"<metadata>{article}</metadata></record></GetRecord></OAI-PMH>"
            )
            return HttpResponse(200, {}, xml.encode())
        kind = "lookup" if "/search?" in url else "xml"
        if self.remaining.get(kind, 0):
            self.remaining[kind] -= 1
            self.urls.append(url)
            return HttpResponse(503, self.headers, b"")
        return super().get(url, **options)


def test_503_during_lookup_and_fulltext_retries_only_the_failed_get(monkeypatch):
    monkeypatch.setattr("citation_tool.citation_sources.time.sleep", lambda _: None)
    transport = AvailabilityTransport(failures={"lookup": 1, "xml": 1})
    assert resolver(transport).resolve(identity(), CLAIM).evidence
    assert len(transport.urls) == 4 and not transport.oai_called


def test_persistent_fulltext_503_can_use_prechecked_pmc_fallback(monkeypatch):
    monkeypatch.setattr("citation_tool.citation_sources.time.sleep", lambda _: None)
    transport = AvailabilityTransport(failures={"xml": 5})
    bundle = resolver(transport).resolve(identity(), CLAIM)
    assert len(transport.urls) == 4 and transport.oai_called
    assert bundle.provenance.api_version == "pmc-oai-v1"
    assert all(
        item.source_url.startswith("https://pmc.ncbi.nlm.nih.gov/") for item in bundle.evidence
    )


async def test_legacy_failed_target_rechecks_without_replacing_history(tmp_path, monkeypatch):
    from citation_tool import citation_texts
    from test_citation_texts import TENANT, make_service, request, wait_status

    service, state, candidate = make_service(tmp_path, transport=PreprintTransport())
    state.candidate_diagnostics[0] = candidate.model_copy(
        update={
            "doi": PREPRINT_DOI,
            "work_id": f"work:{PREPRINT_DOI}",
            "paper_id": "paper:preprint:v1",
        }
    )
    selection = request()
    resolve = service._resolver.resolve

    def old_resolver(*_):
        raise CitationSourceError("CITATION_FULLTEXT_UNAVAILABLE", 422, "Legacy source limit")

    try:
        with monkeypatch.context() as old:
            old.setattr(citation_texts, "PROMPT_VERSION", "citation-text-grounding-v1")
            old.setattr(service._resolver, "resolve", old_resolver)
            previous = await service.start(TENANT, selection, "before-repair")
            failed = await wait_status(service, previous.citation_text_id, {"failed"})
        prepared = service.get_prepared(TENANT, selection)
        assert prepared["source_recheck"] and not prepared["record"]["failure"]["retryable"]
        assert service._resolver.resolve == resolve
        assert (
            await service.start(TENANT, selection, "before-repair")
        ).citation_text_id == failed.citation_text_id
        fresh = await service.start(TENANT, selection, "after-repair")
        answer = await wait_status(service, fresh.citation_text_id, {"ready", "failed"})
        assert answer.status == "ready", answer.failure
        assert answer.source_level == "fulltext_html" and answer.result_version == 3
        assert service.get(TENANT, previous.citation_text_id).status == "failed"
        assert "source_recheck" not in service.get_prepared(TENANT, selection)
    finally:
        await service.aclose()


def test_long_retry_after_does_not_start_an_early_retry():
    transport = AvailabilityTransport(failures={"lookup": 5}, headers={"retry-after": "60"})
    with pytest.raises(CitationSourceError) as caught:
        resolver(transport).resolve(identity(), CLAIM)
    assert caught.value.retryable and len(transport.urls) == 1 and not transport.oai_called
