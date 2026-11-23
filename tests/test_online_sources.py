from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
import pytest

from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import SearchCreate, utc_now_iso
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.online import (
    EuropePMCOnlineAdapter,
    native_query,
    normalize_online_record,
)
from citation_tool.retrieval import SourceSearchError
from citation_tool.runtime import build_metadata_adapters, settings_from_env
from citation_tool.search import _build_claim, library_infos
from test_agent import StubReasonGenerator
from test_integration import headers, wait_final
from test_units import make_claim


def online_record(**updates):
    item = {
        "id": "40060180",
        "source": "MED",
        "pmid": "40060180",
        "doi": "10.1007/S42994-024-00192-2",
        "title": "LEAFY1 and 2 are required for floral organ development in soybean.",
        "abstractText": (
            "Single and double lfy1 lfy2 mutants establish homeotic floral organ identity "
            "in soybean. The study examines floral development and gene expression."
        ),
        "pubYear": "2025",
        "authorString": "Wang L, Liu H",
        "pubTypeList": {"pubType": ["research-article", "Journal Article"]},
    }
    item.update(updates)
    return item


def online_payload(item=None):
    return {
        "version": "6.9",
        "hitCount": 1,
        "resultList": {"result": [item or online_record()]},
    }


def online_library():
    return LibraryConfig(
        "europepmc-online",
        "Europe PMC 在线",
        "",
        0,
        adapter_id="europepmc-online-api-v1",
        simulation=False,
        source_type="remote_api",
    )


def snapshot_library(library_id="europepmc-snapshot", name="多学科文献快照"):
    return LibraryConfig(
        library_id,
        name,
        "",
        0,
        adapter_id="metadata-snapshot-bm25-v1",
        simulation=False,
        source_type="local_snapshot",
        corpus_path=Path("unused-when-adapter-is-injected.json"),
    )


def snapshot_record(url="https://europepmc.org/article/MED/40060180"):
    normalized = normalize_online_record(online_record())
    assert normalized is not None
    return {**normalized, "source_metadata_url": url}


def test_online_native_query_and_normalized_candidate_contract():
    requests = []

    def transport(url, timeout):
        requests.append((url, timeout))
        return online_payload()

    adapter = EuropePMCOnlineAdapter(online_library(), result_limit=10, transport=transport)
    claim = make_claim(text="LFY2 soybean floral organ")
    candidates = adapter.search(claim)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source.doi == "10.1007/s42994-024-00192-2"
    assert candidate.source.adapter_id == "europepmc-online-api-v1"
    assert candidate.source.simulation is False
    assert candidate.passage.index_version.startswith("europepmc-online-response-")
    assert candidate.score_scale_group.startswith("europepmc-online-bm25:")
    assert candidate.retraction_status == "unknown"
    params = parse_qs(urlparse(requests[0][0]).query)
    assert params["resultType"] == ["core"]
    assert params["pageSize"] == ["10"]
    assert 'TITLE_ABS:"LFY2"' in params["query"][0]
    assert 'TITLE_ABS:"soybean"' in params["query"][0]
    assert "HAS_ABSTRACT:y" in params["query"][0]
    assert adapter.model_versions["source_hit_count"] == "1"
    assert adapter.model_versions["source_scope"] == "remote_live_database"


def test_online_relations_retractions_and_stable_non_doi_identity():
    corrected = normalize_online_record(
        online_record(
            commentCorrectionList={
                "commentCorrection": [
                    {"type": "Erratum in", "reference": "10.1007/example-correction"}
                ]
            }
        )
    )
    assert corrected["retraction_status"] == "corrected"
    assert corrected["related_dois"]["corrected_by"] == ["10.1007/example-correction"]
    retracted = normalize_online_record(online_record(retracted="Y"))
    assert retracted["retraction_status"] == "retracted"
    without_doi = normalize_online_record(online_record(doi=None))
    assert without_doi["work_id"] == "work:europepmc:MED:40060180"
    assert without_doi["doi"] is None
    assert normalize_online_record({"title": "missing source identity"}) is None


def test_abstract_topics_are_preserved_without_turning_full_text_into_a_phrase():
    body = SearchCreate(
        input_type="abstract",
        abstract="LFY2 regulates floral organ development in soybean.",
        topic_context=["LFY2", "soybean"],
    )
    claim, _ = _build_claim("local", "q_abstract", body, utc_now_iso(), None)
    assert claim.topic_context == ["LFY2", "soybean"]
    assert 'TITLE_ABS:"LFY2"' in native_query(claim)
    without_topic = claim.model_copy(update={"topic_context": []})
    query = native_query(without_topic)
    assert query.startswith("(LFY2 regulates floral organ development")
    assert 'TITLE_ABS:"LFY2 regulates' not in query
    too_long = without_topic.model_copy(update={"claim_text": "a" * 4001})
    with pytest.raises(SourceSearchError) as error:
        native_query(too_long)
    assert error.value.code == "QUERY_TOO_LONG"


def test_online_transport_failures_are_explicit_and_bounded():
    with pytest.raises(SourceSearchError, match="地址") as error:
        EuropePMCOnlineAdapter._fetch("http://www.ebi.ac.uk/invalid", 1)
    assert error.value.code == "SOURCE_URL_INVALID"

    def invalid_response(url, timeout):
        return {"resultList": {"result": {}}}

    adapter = EuropePMCOnlineAdapter(online_library(), transport=invalid_response)
    with pytest.raises(SourceSearchError) as invalid:
        adapter.search(make_claim())
    assert invalid.value.code == "SOURCE_INVALID_RESPONSE"
    assert "HAS_ABSTRACT:y" in native_query(make_claim())


@pytest.mark.parametrize("payload", [{}, {"resultList": {}}, {"error": "busy"}])
def test_online_missing_result_payload_is_not_relabelled_as_zero_matches(payload):
    adapter = EuropePMCOnlineAdapter(online_library(), transport=lambda *_: payload)
    with pytest.raises(SourceSearchError) as error:
        adapter.search(make_claim())
    assert error.value.code == "SOURCE_INVALID_RESPONSE"


def test_online_empty_result_list_is_valid_but_bad_optional_metadata_does_not_abort():
    adapter = EuropePMCOnlineAdapter(
        online_library(), transport=lambda *_: {"hitCount": 0, "resultList": {"result": []}}
    )
    assert adapter.search(make_claim()) == []

    adapter = EuropePMCOnlineAdapter(
        online_library(),
        transport=lambda *_: online_payload(online_record(pubTypeList="unknown")),
    )
    batch = adapter._reading_page("LFY2", 5)
    assert len(batch.items) == 1
    assert batch.items[0].data["pub_types"] == []


def test_environment_registers_online_large_and_teacher_libraries(tmp_path, monkeypatch):
    record = snapshot_record()
    for name in ("large.json", "sample.json"):
        (tmp_path / name).write_text(
            json.dumps({"records": [record]}, ensure_ascii=False), encoding="utf-8"
        )
    monkeypatch.setenv("CITATION_LIBRARY_MODE", "metadata")
    monkeypatch.setenv("CITATION_CORPUS_PATH", str(tmp_path / "large.json"))
    monkeypatch.setenv("CITATION_SAMPLE_CORPUS_PATH", str(tmp_path / "sample.json"))
    monkeypatch.setenv("CITATION_EUROPEPMC_ONLINE", "1")
    monkeypatch.setenv("CITATION_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CITATION_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("CITATION_MODEL_CONFIG", str(tmp_path / "missing-model.json"))
    monkeypatch.setenv("CITATION_EMBEDDING_ENABLED", "1")
    monkeypatch.delenv("CITATION_DEMO_CONTINUITY", raising=False)
    settings = settings_from_env()
    assert not settings.demo_continuity
    assert [library.library_id for library in settings.libraries] == [
        "europepmc-online",
        "europepmc-snapshot",
        "abiotech-sample",
    ]
    adapters = build_metadata_adapters(settings)
    assert set(adapters) == {library.library_id for library in settings.libraries}
    assert adapters["europepmc-snapshot"]._top_k == 10
    pointwise = build_metadata_adapters(replace(settings, candidate_pipeline="pointwise"))
    assert pointwise["europepmc-snapshot"]._top_k == 5
    app = create_app(settings, adapters=adapters)
    infos = library_infos(app.state.settings)
    assert [item.record_count for item in infos] == [None, 1, 1]
    assert [item.source_type for item in infos] == [
        "remote_api",
        "local_snapshot",
        "local_snapshot",
    ]


@pytest.mark.parametrize("cache_state", ["missing", "corrupt", "wrong-records", "ready"])
def test_metadata_factory_requires_a_prebuilt_embedding_index(tmp_path, cache_state):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"records": [snapshot_record()]}), encoding="utf-8")
    library = LibraryConfig(
        "local",
        "Local corpus",
        "",
        0,
        simulation=False,
        adapter_id="metadata-hybrid-e5-rrf-v1",
        source_type="local_snapshot",
        corpus_path=corpus,
    )
    cache = tmp_path / "cache"
    settings = replace(
        Settings.for_tests(tmp_path),
        library_mode="metadata",
        libraries=(library,),
        embedding_enabled=True,
        embedding_cache_dir=cache,
    )
    snapshot = cache / "models--intfloat--multilingual-e5-small" / "snapshots" / "fixture-revision"
    onnx = snapshot / "onnx" / "model.onnx"
    onnx.parent.mkdir(parents=True)
    onnx.touch()
    index = build_metadata_adapters(settings)["local"]._index
    if cache_state == "corrupt":
        index.cache_path.write_bytes(b"not a matrix")
    elif cache_state in {"wrong-records", "ready"}:
        np.savez(
            index.cache_path,
            version=index._cache_identity["version"],
            model=index.model_name,
            model_revision=index.model_revision,
            ids=index._ids if cache_state == "ready" else ["wrong-work-id"],
            vectors=np.asarray([[1.0, 0.0]], dtype=np.float32),
        )

    if cache_state == "ready":
        app = create_app(settings)
        loaded = app.state.adapters["local"]._index
        assert loaded._model is None
        assert loaded._prebuilt_model_path == snapshot
        assert np.array_equal(loaded._load_matrix(), [[1.0, 0.0]])
    else:
        with pytest.raises(RuntimeError, match="Missing or invalid prebuilt E5 cache"):
            create_app(settings)
        app = create_app(replace(settings, embedding_enabled=False))
        assert type(app.state.adapters["local"]) is MetadataCorpusAdapter


def test_three_source_workers_merge_dois_without_mixing_score_scales(tmp_path, live_server):
    online = online_library()
    large = snapshot_library()
    sample = snapshot_library("abiotech-sample", "aBiotech 老师样例库")
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(online, large, sample),
        fixture_demo_scope_default=False,
    )
    adapters = {
        online.library_id: EuropePMCOnlineAdapter(
            online, transport=lambda url, timeout: online_payload()
        ),
        large.library_id: MetadataCorpusAdapter(
            large, [snapshot_record("https://example.org/large-snapshot")]
        ),
        sample.library_id: MetadataCorpusAdapter(
            sample, [snapshot_record("https://example.org/teacher-sample")]
        ),
    }
    base = live_server(
        create_app(settings, adapters=adapters, reason_generator=StubReasonGenerator())
    )
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json={"input_type": "keywords", "keywords": ["LFY1", "soybean", "floral"]},
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
    assert state["session"]["search_state"] == "completed"
    assert len(state["workers"]) == 3
    assert {item["library_id"] for item in state["workers"]} == set(adapters)
    assert len(state["candidate_diagnostics"]) == 1
    candidate = state["candidate_diagnostics"][0]
    assert candidate["library_ids"] == ["abiotech-sample", "europepmc-online", "europepmc-snapshot"]
    assert set(candidate["source_uris"]) == set(adapters)
    assert candidate["score_status"] == "scoped"
    assert candidate["candidate_score"] == 100
    assert candidate["reason"]["status"] == "generated"
    assert len(candidate["retrieval_scores"]) == 3
    assert {item["library_id"] for item in candidate["retrieval_scores"]} == set(adapters)


def test_online_failure_does_not_block_local_snapshots(tmp_path, live_server):
    online = online_library()
    sample = snapshot_library("abiotech-sample", "aBiotech 老师样例库")
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(online, sample),
        fixture_demo_scope_default=False,
    )

    def limited(url, timeout):
        raise SourceSearchError("SOURCE_RATE_LIMITED", "Europe PMC 请求触发限流，请稍后重试。")

    adapters = {
        online.library_id: EuropePMCOnlineAdapter(online, transport=limited),
        sample.library_id: MetadataCorpusAdapter(sample, [snapshot_record()]),
    }
    app = create_app(settings, adapters=adapters, reason_generator=StubReasonGenerator())
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json={"input_type": "keywords", "keywords": ["LFY1", "soybean", "floral"]},
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
    workers = {worker["library_id"]: worker for worker in state["workers"]}
    assert state["session"]["search_state"] == "partial"
    assert workers[online.library_id]["last_error"]["code"] == "SOURCE_RATE_LIMITED"
    assert workers[sample.library_id]["worker_state"] == "completed"
    assert len(state["candidate_diagnostics"]) == 1
    assert state["candidate_diagnostics"][0]["library_ids"] == [sample.library_id]
