from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.citation_sources import (
    CitationSourceError,
    EuropePmcSourceResolver,
    HttpResponse,
)
from citation_tool.config import LibraryConfig, Settings
from citation_tool.literature_reader import LiteratureReader
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.online import EuropePMCOnlineAdapter
from test_agent import ScriptedChatModel

DOI = "10.5555/test.reader"
PRIVATE_QUERY = "observed values"
TITLE = "Goldleaf demonstration article"
TEXT = "The observed values were large. This was a randomized comparison."


def local(library_id, tmp_path, **changes):
    config = LibraryConfig(
        library_id,
        library_id,
        "",
        0,
        simulation=False,
        source_type="local_snapshot",
        corpus_path=tmp_path / "not-used.json",
    )
    record = {
        "work_id": f"work:{DOI}",
        "paper_id": f"paper:{library_id}:1",
        "title": TITLE,
        "abstract": TEXT,
        "doi": DOI,
        "year": 2025,
        "pmcid": "PMC1234567",
        "pmid": "12345678",
        "source_metadata_url": f"https://doi.org/{DOI}",
        "source_pdf": "private-archive-path.pdf",
    } | changes
    return config, record


def local_app(tmp_path, model=None, *, enabled=True, extra=()):
    one, row = local("one", tmp_path)
    two, other = local("two", tmp_path, abstract="A different version, not merged.")
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(one, two),
        agent_literature_reader=enabled,
    )
    return create_app(
        settings,
        adapters={
            "one": MetadataCorpusAdapter(one, [row, *extra]),
            "two": MetadataCorpusAdapter(two, [other]),
        },
        agent_model=model,
    )


async def test_local_find_open_verify_and_turn_scope(tmp_path):
    app = local_app(tmp_path)
    async with app.router.lifespan_context(app):
        reader = LiteratureReader(app.state.search_service, ["one"])
        tool = reader.as_tool()
        found = await tool.ainvoke(
            {
                "action": "discover",
                "text": PRIVATE_QUERY,
                "literal": True,
                "limit": 20,
            }
        )
        assert found["upstream_hit_count"] == 1
        assert found["page_size"] == 12
        (lead,) = found["leads"]
        assert lead["lead"]["kind"] == "literal_match"
        assert "source_pdf" not in json.dumps(lead)
        assert reader.mentioned_source_texts(DOI)[0]["source_text"] == ""
        opened = await tool.ainvoke({"action": "open", "source_ref": lead["source_ref"]})
        assert opened["passages"][0]["text"] == TEXT
        assert opened["next_window"] is None
        assert opened["level"] == "abstract"
        locator = {"text_ref": opened["text_ref"], "text": "OBSERVED VALUES"}
        found = await tool.ainvoke({"action": "find", **locator})
        assert found["matches"][0]["quote"] == PRIVATE_QUERY
        assert (await tool.ainvoke({"action": "verify", **locator}))["matched"] is False
        valid = await tool.ainvoke({"action": "verify", **locator, "text": PRIVATE_QUERY})
        match = valid["matches"][0]
        assert TEXT[match["match_start"] : match["match_end"]] == PRIVATE_QUERY
        denied = await tool.ainvoke({"action": "open", "library_id": "two", "identifier": DOI})
        assert denied["error"]["code"] == "LIBRARY_NOT_ALLOWED"
        full = await tool.ainvoke({"action": "open", "identifier": DOI, "level": "full_text"})
        assert full["error"]["code"] == "LOCAL_ABSTRACT_ONLY"
        assert full["abstract_available"] is True and "text_ref" not in full
        assert reader.dois == {DOI}
        assert reader.mentioned_source_texts(DOI)[0]["abstract"] == TEXT
        another = LiteratureReader(app.state.search_service, ["one"]).as_tool()
        assert (await another.ainvoke({"action": "open", "source_ref": lead["source_ref"]}))[
            "error"
        ]["code"] == "SOURCE_REF_UNKNOWN"
        assert (await another.ainvoke({"action": "read", "text_ref": opened["text_ref"]}))["error"][
            "code"
        ] == "TEXT_REF_UNKNOWN"


async def test_discovery_pagination_and_ambiguous_identifier_do_not_mix_versions(tmp_path):
    _, second = local("one", tmp_path, paper_id="paper:version-B", abstract=TEXT + " 版本二。")
    app = local_app(tmp_path, extra=[second])
    async with app.router.lifespan_context(app):
        tool = LiteratureReader(app.state.search_service, ["one"]).as_tool()
        first = await tool.ainvoke({"action": "discover", "text": PRIVATE_QUERY, "limit": 1})
        next_page = await tool.ainvoke({"action": "discover", "cursor": first["next_cursor"]})
        assert next_page["next_cursor"] is None
        assert first["leads"][0]["source_ref"] != next_page["leads"][0]["source_ref"]
        ambiguous = await tool.ainvoke({"action": "open", "identifier": DOI})
        assert ambiguous["error"]["code"] == "SOURCE_AMBIGUOUS"
        opened = await tool.ainvoke(
            {"action": "open", "source_ref": next_page["leads"][0]["source_ref"]}
        )
        assert opened["passages"][0]["text"].endswith("版本二。")


class PublicArticleTransport:
    def __init__(self, **overrides):
        self.record = {
            "id": "12345678",
            "source": "MED",
            "pmid": "12345678",
            "pmcid": "PMC1234567",
            "doi": DOI,
            "title": TITLE,
            "abstractText": "",
            "isOpenAccess": "Y",
            "inEPMC": "Y",
            "license": "cc by",
            "pubYear": "2025",
        } | overrides
        self.xml = (
            f"""<article><front><article-meta>
        <article-id pub-id-type="pmc">1234567</article-id>
        <article-id pub-id-type="doi">{DOI}</article-id>
        <title-group><article-title>{TITLE}</article-title></title-group>
        </article-meta></front><body>
        <sec id="m"><title>Methods</title><p id="short">Four groups.</p>
        <p id="long">{("é🦉" * 11_500)} stop.</p>
        <list><list-item>
          <p>The sequence matches here. The sequence matches again.</p>
        </list-item></list>
        <table-wrap><label>Table 1</label>
          <table><tr><td>abc</td><td>def</td></tr></table>
        </table-wrap>
        </sec></body><back><ack><p>Thanks for the check.</p></ack></back></article>"""
        ).encode()
        self.downloads = 0
        self.search_requests = []

    def search(self, url, timeout):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "www.ebi.ac.uk"
        self.search_requests.append(parse_qs(parsed.query))
        return {"resultList": {"result": [self.record]}, "hitCount": 1}

    def get(self, url, *, accept, timeout_s, max_bytes):
        assert url == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1234567/fullTextXML"
        self.downloads += 1
        return HttpResponse(200, {}, self.xml)


def online_reader(tmp_path, transport):
    lib = LibraryConfig("online", "Online", "", 0, simulation=False, source_type="remote_api")
    adapter = EuropePMCOnlineAdapter(lib, transport=transport.search)
    app = create_app(
        replace(Settings.for_tests(tmp_path), libraries=(lib,)), adapters={"online": adapter}
    )
    return app, LiteratureReader(
        app.state.search_service,
        ["online"],
        resolver=EuropePmcSourceResolver(transport),
    )


async def test_online_fulltext_without_abstract_and_complete_lossless_pagination(tmp_path):
    source = PublicArticleTransport()
    app, reader = online_reader(tmp_path, source)
    async with app.router.lifespan_context(app):
        tool = reader.as_tool()
        opened = await tool.ainvoke(
            {"action": "open", "identifier": f"https://doi.org/{DOI}", "level": "full_text"}
        )
        assert opened["level"] == "full_text" and "passages" not in opened
        assert "HAS_ABSTRACT" not in source.search_requests[0]["query"][0]
        assert opened["provenance"]["sha256"] == hashlib.sha256(source.xml).hexdigest()
        assert reader.mentioned_source_texts(DOI)[0]["source_text"] == ""
        text_ref = opened["text_ref"]
        first = await tool.ainvoke(
            {"action": "read", "text_ref": text_ref, **opened["next_window"]}
        )
        assert first["next_window"]["char_offset"] > 0
        tail = await tool.ainvoke({"action": "read", "text_ref": text_ref, **first["next_window"]})
        assert tail["next_window"] is None
        rows = first["passages"] + tail["passages"]
        assert (
            "".join(row["text"] for row in rows if row["element_id"] == "long")
            == "é🦉" * 11_500 + " stop."
        )
        assert any(row["text"] == "Four groups." for row in rows)
        assert any(row["text"] == "abc\tdef" for row in rows)
        assert any(row["text"] == "Thanks for the check." for row in rows)
        locate = {
            "action": "verify",
            "text_ref": text_ref,
            "text": "sequence matches",
            "limit": 1,
            "level": "full_text",
            "library_id": "online",
            "identifier": DOI,
            "source_ref": opened["source"]["source_ref"],
        }
        hit = await tool.ainvoke(locate)
        mismatch = await tool.ainvoke({**locate, "identifier": "10.5555/other"})
        assert mismatch["error"]["code"] == "SOURCE_BINDING_MISMATCH"
        next_hit = await tool.ainvoke({**locate, **hit["next_window"]})
        assert hit["matches"][0]["match_start"] < next_hit["matches"][0]["match_start"]
        assert next_hit["next_window"] is None
        assert source.downloads == 1
        assert (
            await tool.ainvoke({"action": "open", "identifier": "https://untrusted.example/PDF"})
        )["error"]["code"] == "SOURCE_ID_INVALID"
        assert len(source.search_requests) == 1


@pytest.mark.parametrize(
    "danger,code,downloads",
    [
        ("non_open", "CITATION_SOURCE_NOT_OPEN", 0),
        ("retracted", "CITATION_RETRACTED", 0),
        ("version", "CITATION_IDENTITY_MISMATCH", 1),
        ("unsafe_xml", "CITATION_SOURCE_UNSAFE_XML", 1),
    ],
)
async def test_online_fulltext_fails_closed(tmp_path, danger, code, downloads):
    source = PublicArticleTransport()
    if danger == "non_open":
        source.record["isOpenAccess"] = "N"
    if danger == "retracted":
        source.record["retracted"] = "Y"
    if danger == "version":
        source.xml = source.xml.replace(b">1234567<", b">7654321<")
    if danger == "unsafe_xml":
        source.xml = '<!DOCTYPE article [<!ENTITY e "BAD">]><article>&e;</article>'.encode("utf-16")
    app, reader = online_reader(tmp_path, source)
    async with app.router.lifespan_context(app):
        answer = await reader.as_tool().ainvoke(
            {"action": "open", "identifier": DOI, "level": "full_text"}
        )
        assert answer["error"]["code"] == code
        assert "text_ref" not in answer
        assert source.downloads == downloads


async def test_official_oai_fallback_preserves_identity_after_transient_error(tmp_path):
    class TransientEuropePMC(PublicArticleTransport):
        def get(self, url, **kwargs):
            host = urlsplit(url).hostname
            if host == "www.ebi.ac.uk":
                raise CitationSourceError("CITATION_SOURCE_TIMEOUT", 504, "timeout", retryable=True)
            assert host == "pmc.ncbi.nlm.nih.gov"
            assert parse_qs(urlsplit(url).query) == {
                "verb": ["GetRecord"],
                "metadataPrefix": ["pmc"],
                "identifier": ["oai:pubmedcentral.nih.gov:1234567"],
            }
            self.downloads += 1
            payload = (
                b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                b"<GetRecord><record><header><identifier>oai:pubmedcentral.nih.gov:1234567"
                b"</identifier></header><metadata>"
                + self.xml
                + b"</metadata></record></GetRecord></OAI-PMH>"
            )
            return HttpResponse(200, {}, payload)

    source = TransientEuropePMC()
    app, reader = online_reader(tmp_path, source)
    async with app.router.lifespan_context(app):
        data = await reader.as_tool().ainvoke(
            {
                "action": "open",
                "identifier": "PMC1234567",
                "level": "full_text",
            }
        )
    assert data["provenance"]["provider"] == "pmc_oai"
    assert data["provenance"]["fallback_code"] == "CITATION_SOURCE_TIMEOUT"
    assert source.downloads == 1


async def test_turn_budget_and_invalid_requests_do_not_loop(tmp_path, monkeypatch):
    from citation_tool import literature_reader as module

    monkeypatch.setattr(module, "MAX_RUN_CHARS", 10)
    app = local_app(tmp_path)
    async with app.router.lifespan_context(app):
        tool = LiteratureReader(app.state.search_service, ["one"]).as_tool()
        assert isinstance(await tool.ainvoke({"action": "verify"}), str)
        first = await tool.ainvoke({"action": "open", "identifier": DOI})
        assert first["next_window"] == {"offset": 0, "char_offset": 10}
        second = await tool.ainvoke(
            {"action": "read", "text_ref": first["text_ref"], **first["next_window"]}
        )
        assert second["error"]["code"] == "READER_TEXT_LIMIT"
        assert "next_window" not in second


@pytest.mark.parametrize("enabled,terminal", [(True, "completed"), (False, "failed")])
async def test_agent_can_answer_from_same_turn_original_without_starting_a_ranking(
    tmp_path, enabled, terminal
):
    answer = AIMessage(content=f"{TITLE} ({DOI}) reported a randomized comparison.")
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "read-real-record",
                        "name": "read_literature",
                        "args": {"action": "open", "identifier": "PMID:12345678"},
                    }
                ],
            ),
            answer,
            *([] if enabled else [AIMessage(content=answer.content)]),
        ]
    )
    app = local_app(tmp_path, model, enabled=enabled)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        started = await client.post(
            "/api/v1/agent/runs",
            json={
                "input_type": "keywords",
                "keywords": ["find trial design"],
                "library_ids": ["one"],
            },
        )
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        task = app.state.agent_runner._tasks.get(run_id)
        if task is not None:
            await asyncio.wait_for(task, timeout=10)
        data = (await client.get(f"/api/v1/agent/runs/{run_id}")).json()
        assert data["status"] == terminal, data.get("error")
        assert data["queries"] == []
        assert data["latest_query_id"] is None
        if enabled:
            manifest = data["context_manifest"]["literature_reader"]["observations"]
            assert manifest[0]["paper_id"] == "paper:one:1"
            assert manifest[1]["spans"][0]["sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
            assert TEXT not in json.dumps(manifest)
            assert data["tool_calls"][0]["status"] == "completed"
        else:
            assert data["error"]["source_dois"] == [DOI]
