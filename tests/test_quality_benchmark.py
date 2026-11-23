from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
import tools.quality_benchmark.__main__ as quality_cli
from tools.benchmark.core import DIMENSIONS, UsageTotals, file_hash
from tools.benchmark.evaluation import PairJudgment
from tools.quality_benchmark import core as quality_core
from tools.quality_benchmark.core import (
    artifact_hash,
    evaluation_records,
    expanded_tasks,
    kimi_prompt,
    load_suite,
    product_request,
    project_skill_isolation_valid,
    validate_gold,
)
from tools.quality_benchmark.evaluation import (
    _only_known_dois,
    automatic_score,
    render_report,
    render_stability_summary,
    stability_summary,
    summarize,
)
from tools.quality_benchmark.ltr_eval import fit

from citation_tool.config import LibraryConfig
from citation_tool.entities import utc_now_iso
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.search import _build_claim
from conftest import require_runtime_file


def load_corpus(suite: dict) -> dict:
    require_runtime_file(quality_core.ROOT / suite["corpus_path"])
    return quality_core.load_corpus(suite)


def _task(case_id="rare_lfy"):
    return next(task for task in expanded_tasks(load_suite()) if task["case_id"] == case_id)


def _core_records():
    suite = load_suite()
    core = set(suite.get("core_work_ids", []))
    return [
        record for record in load_corpus(suite)["records"] if not core or record["work_id"] in core
    ]


def _record(task, **updates):
    value = {
        "system": "product",
        "case_id": task["case_id"],
        "run_case_id": task["run_case_id"],
        "repetition": 1,
        "split": task["split"],
        "completed": True,
        "response_text": "仅依据真实摘要推荐，尚未核查全文。",
        "visible_output": "",
        "model_valid": True,
        "secret_leak": False,
        "retrieval_actions": 1,
        "usage": UsageTotals().as_dict(),
        "wall_time_ms": 1000,
        "queries": [],
        "reason_metrics": {},
    }
    value.update(updates)
    return value


def test_quality_suite_has_disjoint_gold_and_holdout() -> None:
    suite = load_suite()
    corpus = load_corpus(suite)
    all_tasks = expanded_tasks(suite)
    tuning = expanded_tasks(suite, "tuning")
    holdout = expanded_tasks(suite, "holdout")
    development = expanded_tasks(suite, "development")
    assert len(all_tasks) == 42
    assert len(tuning) == 27
    assert len(development) == 7
    assert len(holdout) == 8
    assert {task["case_id"] for task in tuning}.isdisjoint({task["case_id"] for task in holdout})
    validate_gold(all_tasks, corpus)
    assert project_skill_isolation_valid(
        {"agents_md_paths": ["C:/Users/tester/.kimi-code/AGENTS.md"]}
    )
    assert not project_skill_isolation_valid(
        {"agents_md_paths": ["C:/Users/tester/Desktop/文献推荐工具/AGENTS.md"]}
    )
    assert not project_skill_isolation_valid(
        {
            "agents_md_paths": [
                "C:/Users/tester/Desktop/文献推荐工具/.agents/skills/recommend-literature/SKILL.md"
            ]
        }
    )


def test_abstract_product_input_keeps_the_full_natural_language_request() -> None:
    task = _task("genotype_contrast")
    request = product_request(task)
    assert request.abstract == task["request"]["abstract"]
    assert request.topic_context == [task["baseline_query"]]
    prompt = kimi_prompt(task)
    assert task["baseline_query"] in prompt
    assert task["request"]["abstract"] in prompt
    assert task["expect"]["required_ids"][0] not in prompt


def test_product_rank_metrics_use_each_query_not_global_score_comparison() -> None:
    task = _task()
    target = task["expect"]["required_ids"][0]
    record = _record(
        task,
        visible_output=f"真实摘要候选 {target}，未核查全文。",
        queries=[
            {
                "candidate_diagnostics": [
                    {"work_id": "work:10.1007/s42994-024-00193-1"},
                    {"work_id": target},
                ]
            }
        ],
    )
    scored = automatic_score(record, task, load_corpus(load_suite())["records"])
    assert scored["hit_at_5"] is True
    assert math.isclose(scored["ndcg_at_5"], 1 / math.log2(3))
    assert scored["expected_pass"] is True
    record["queries"][0]["candidate_diagnostics"].reverse()
    record["retrieval_actions"] = 2
    record["queries"].append(record["queries"][0])
    scored = automatic_score(record, task, load_corpus(load_suite())["records"])
    assert scored["ndcg_at_5"] == 1


def test_quality_dois_accept_actual_correction_reference_but_not_fabrication() -> None:
    corpus = load_corpus(load_suite())["records"]
    correction = next(record for record in corpus if record["doi"].endswith("00191-3"))
    text = f"{correction['title']} {correction['abstract']} {correction['doi']}"
    assert _only_known_dois(text, corpus)
    assert not _only_known_dois("虚构 DOI 10.9999/not-in-corpus", corpus)


def test_excluded_doi_explanation_requires_semantic_review_not_string_penalty() -> None:
    task = _task("correction_exclusion")
    corpus = load_corpus(load_suite())["records"]
    record = _record(
        task,
        response_text=(
            "仅依据真实摘要推荐原始研究；DOI 10.1007/s42994-025-00250-3 "
            "是其更正声明，不是第二项实验；尚未核查全文。"
        ),
    )
    scored = automatic_score(record, task, corpus)
    assert scored["checks"]["excluded_dois_in_answer"] is None
    assert "excluded_dois_in_answer" not in scored["failed_checks"]


def test_zero_answer_metrics_do_not_fabricate_recall_or_pass_causal_judgment() -> None:
    task = _task("inversion_causality_limit")
    record = _record(task)
    scored = automatic_score(record, task, load_corpus(load_suite())["records"])
    assert scored["ndcg_at_5"] is None
    assert scored["hit_at_5"] is None
    assert "required_candidates" not in scored["failed_checks"]


def test_critical_judgment_flags_require_verbatim_output_evidence() -> None:
    judgment = PairJudgment.model_validate(
        {
            "dimensions": [
                {
                    "dimension": dimension,
                    "score_a": 1,
                    "score_b": 2,
                    "winner": "B",
                    "rationale": "test",
                }
                for dimension in DIMENSIONS
            ],
            "flags_a": ["metadata_overclaim"],
            "flags_b": [],
            "flag_evidence_a": ["摘要已核验全文"],
            "flag_evidence_b": [],
            "summary": "test",
        }
    )
    judgment.validate_flag_evidence("该回答声称摘要已核验全文。", "")
    try:
        judgment.validate_flag_evidence("该回答只使用摘要。", "")
    except ValueError as exc:
        assert "exact excerpt" in str(exc)
    else:
        raise AssertionError("invented flag evidence was accepted")


def test_blind_label_mapping_is_not_reversed() -> None:
    task = _task()
    record = _record(task)
    record["automatic"] = automatic_score(record, task, load_corpus(load_suite())["records"])
    judgment = {
        "run_case_id": task["run_case_id"],
        "status": "completed",
        "label_mapping": {"A": "product", "B": "kimi_code"},
        "judgment": {
            "dimensions": [
                {
                    "dimension": dimension,
                    "score_a": 1,
                    "score_b": 2,
                    "winner": "B",
                    "rationale": "test",
                }
                for dimension in DIMENSIONS
            ],
            "flags_a": [],
            "flags_b": [],
            "summary": "test",
        },
    }
    summary = summarize([record], [judgment], UsageTotals().as_dict())
    assert summary["blind_dimensions"]["relevance_completion"]["product_mean"] == 1
    assert summary["blind_dimensions"]["relevance_completion"]["kimi_mean"] == 2
    assert summary["blind_dimensions"]["relevance_completion"]["wins"]["kimi_code"] == 1


def test_original_keywords_survive_natural_language_context() -> None:
    task = _task("correction_exclusion")
    claim, _ = _build_claim(
        "quality-test", "q-original-keywords", product_request(task), utc_now_iso(), None
    )
    assert claim.query_terms == task["request"]["keywords"]
    library = LibraryConfig("abiotech-real", "aBiotech", "fast", 1, simulation=False)
    adapter = MetadataCorpusAdapter(library, _core_records())
    candidates = adapter.search(claim)
    assert task["expect"]["required_ids"][0] in {
        candidate.passage.work_id for candidate in candidates
    }
    assert candidates[0].passage.work_id == task["expect"]["required_ids"][0]
    assert all(not candidate.passage.work_id.endswith("00250-3") for candidate in candidates)


def test_judge_receives_required_background_and_forbidden_evidence() -> None:
    task = _task("review_identity")
    corpus = load_corpus(load_suite())["records"]
    evidence = evaluation_records(task, corpus)
    expected = set(task["expect"]["required_ids"]) | set(task["expect"]["background_ids"])
    assert {record["work_id"] for record in evidence} == expected
    assert all(record["abstract"] for record in evidence)


def test_exclusion_clause_keeps_target_at_top_without_impossible_identifier_and() -> None:
    task = _task("rare_lfy")
    request = product_request(task).model_copy(
        update={
            "topic_context": [
                "在大豆中寻找 LEAFY/LFY1/LFY2 控制花器官形成的原始功能研究，"
                "排除仅分析开花期关联的论文"
            ]
        }
    )
    claim, _ = _build_claim("quality-test", "q-negation-clause", request, utc_now_iso(), None)
    library = LibraryConfig("abiotech-real", "aBiotech", "fast", 1, simulation=False)
    adapter = MetadataCorpusAdapter(library, _core_records())
    candidates = adapter.search(claim)
    assert candidates[0].passage.work_id == task["expect"]["required_ids"][0]
    assert all(
        candidate.display_score < candidates[0].display_score for candidate in candidates[1:]
    )


async def test_rescore_reuses_outputs_and_preserves_original_history(tmp_path, monkeypatch) -> None:
    task = _task()
    corpus = load_corpus(load_suite())
    monkeypatch.setattr(quality_cli, "RESULTS_ROOT", tmp_path / "results")
    monkeypatch.setattr(quality_cli, "QUALITY_RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(quality_cli, "expanded_tasks", lambda suite, split: [task])
    monkeypatch.setattr(quality_cli, "artifact_hash", lambda: "new-scorer-hash")
    monkeypatch.setattr(quality_cli, "model_secrets", lambda: ["unit-test-not-a-secret"])
    source = tmp_path / "results" / "source-run"
    source.mkdir(parents=True)
    plan = {
        "run_name": "source-run",
        "created_at": "2026-11-15T00:00:00Z",
        "tasks_sha256": file_hash(quality_cli.SUITE_PATH),
        "corpus_sha256": corpus["corpus_sha256"],
        "implementation_hash": "original-product-hash",
        "selection": "tuning",
    }
    (source / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    target = task["expect"]["required_ids"][0]
    records = []
    for system in ("product", "kimi_code"):
        record = _record(
            task,
            system=system,
            visible_output=f"真实摘要候选 {target}；没有全文定位。",
            queries=[
                {"candidate_diagnostics": [{"work_id": target}]} if system == "product" else []
            ],
            agent_run_id="agt-source" if system == "product" else None,
            session_id="session-source" if system == "kimi_code" else None,
            agents_md_paths=[] if system == "kimi_code" else None,
            tool_calls=[
                {"name": "Bash", "arguments": {"command": "ls corpus.json"}},
                {
                    "name": "Bash",
                    "arguments": {
                        "command": "head -n 1 corpus.json && echo 10.1007/s42994-024-00192-2"
                    },
                },
            ]
            if system == "kimi_code"
            else [],
            retrieval_actions=2 if system == "kimi_code" else 1,
        )
        records.append(record)
        path = tmp_path / "runtime" / "source-run" / "records"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{task['run_case_id']}.{system}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    async def fake_judge(records, tasks, corpus, *, concurrency, corpus_context):
        assert corpus_context["records"] is corpus
        assert "governance_scope" in corpus_context
        judgment = {
            "run_case_id": task["run_case_id"],
            "status": "completed",
            "label_mapping": {"A": "product", "B": "kimi_code"},
            "judgment": {
                "dimensions": [
                    {
                        "dimension": dimension,
                        "score_a": 2,
                        "score_b": 2,
                        "winner": "tie",
                        "rationale": "test",
                    }
                    for dimension in DIMENSIONS
                ],
                "flags_a": [],
                "flags_b": [],
                "summary": "test",
            },
        }
        return [judgment], UsageTotals().as_dict()

    monkeypatch.setattr(quality_cli, "judge_records", fake_judge)
    result = await quality_cli.rescore(
        SimpleNamespace(
            source_name="source-run",
            name="rescored-run",
            reason="test metric repair",
            concurrency=1,
        )
    )
    assert result == 0
    new_plan = json.loads(
        (tmp_path / "results" / "rescored-run" / "plan.json").read_text(encoding="utf-8")
    )
    assert new_plan["implementation_hash"] == "original-product-hash"
    assert new_plan["scoring_implementation_hash"] == "new-scorer-hash"
    assert new_plan["original_agent_calls_repeated"] is False
    scored = [
        json.loads(line)
        for line in (tmp_path / "results" / "rescored-run" / "runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    kimi = next(record for record in scored if record["system"] == "kimi_code")
    assert kimi["raw_retrieval_actions_before_metric_v2"] == 2
    assert kimi["retrieval_actions"] == 1
    assert kimi["project_skill_isolation_valid"] is True
    original = json.loads(
        (tmp_path / "runtime" / "source-run" / "records" / "rare_lfy.kimi_code.json").read_text(
            encoding="utf-8"
        )
    )
    assert original["retrieval_actions"] == 2
    assert "raw_retrieval_actions_before_metric_v2" not in original


def test_ltr_fit_learns_positive_margin_deterministically() -> None:
    example = [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]] * 32
    first = fit(example)
    assert first[0] > 0
    assert first == fit(example)
    assert sum(weight * feature for weight, feature in zip(first, example[0], strict=True)) > 0


def _external_suite(tmp_path, corpus):
    tasks = []
    for index in range(16):
        required = [] if index >= 14 else [f"work:{corpus['records'][index]['doi']}"]
        if index == 0:
            required.append(f"work:{corpus['records'][16]['doi']}")
        forbidden = [f"work:{corpus['records'][index + 20]['doi']}"]
        anchored = [*required, *forbidden]
        tasks.append(
            {
                "case_id": f"external_{index}",
                "slice": "semantic_ood",
                "baseline_query": f"请完成第 {index} 个独立研究候选发现请求并说明证据边界。",
                "request": (
                    {
                        "input_type": "abstract",
                        "abstract": (
                            f"Independent research context {index} with no copied source text."
                        ),
                    }
                    if index % 2
                    else {"input_type": "keywords", "keywords": [f"concept {index}"]}
                ),
                "expect": {
                    "required_ids": required,
                    "forbidden_ids": forbidden,
                    "gold_note": "Synthetic structure test only; not a scientific gold label.",
                    "evidence_anchors": [
                        {
                            "work_id": work_id,
                            "quote": next(
                                record["abstract"][:120]
                                for record in corpus["records"]
                                if record["work_id"] == work_id
                            ),
                            "supports": "structure fixture",
                        }
                        for work_id in anchored
                    ],
                },
            }
        )
    suite = {
        "suite_id": "astra-independent-abstract-v1",
        "corpus_path": "data/runtime/quality-corpus/large-corpus.json",
        "max_retrieval_rounds": 3,
        "repetitions": 3,
        "stability_case_ids": [f"external_{index}" for index in range(4)],
        "tuning_case_ids": [],
        "development_case_ids": [],
        "tasks": tasks,
    }
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(suite), encoding="utf-8")
    return suite, path


async def test_hybrid_agent_run_requires_prebuilt_embedding_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(quality_cli, "load_corpus", load_corpus)
    monkeypatch.setattr(quality_cli, "EMBEDDING_CACHE_DIR", tmp_path / "missing-cache")
    args = SimpleNamespace(
        suite=quality_cli.ROOT / "benchmark/external/astra-v1/tasks.json",
        protocol=quality_cli.ROOT / "benchmark/external/astra-v1/protocol.md",
        split="holdout",
        cases="rare_peanut_pugn",
        retriever="hybrid",
        name="must-not-run",
        concurrency=1,
        resume=False,
    )
    try:
        await quality_cli.run(args)
    except ValueError as exc:
        assert "prebuilt embedding cache" in str(exc)
    else:
        raise AssertionError("hybrid Agent run silently rebuilt an entire corpus")


def test_external_suite_is_all_holdout_and_requires_verbatim_anchors(tmp_path) -> None:
    corpus = load_corpus(load_suite())
    suite, path = _external_suite(tmp_path, corpus)
    loaded = load_suite(path)
    tasks = expanded_tasks(loaded)
    assert len(tasks) == 24
    assert len(expanded_tasks(loaded, "holdout")) == 24
    assert expanded_tasks(loaded, "tuning") == []
    validate_gold(tasks, corpus)
    protocol = tmp_path / "protocol.md"
    protocol.write_text("external read-only protocol", encoding="utf-8")
    assert artifact_hash(path, protocol)

    suite["tasks"][0]["expect"]["evidence_anchors"][0]["quote"] = "not a verbatim source quote"
    path.write_text(json.dumps(suite), encoding="utf-8")
    try:
        validate_gold(expanded_tasks(load_suite(path)), corpus)
    except ValueError as exc:
        assert "non-verbatim" in str(exc)
    else:
        raise AssertionError("a fabricated evidence anchor was accepted")


def test_external_suite_rejects_target_identifier_leakage(tmp_path) -> None:
    corpus = load_corpus(load_suite())
    suite, path = _external_suite(tmp_path, corpus)
    target = suite["tasks"][0]["expect"]["required_ids"][0]
    suite["tasks"][0]["baseline_query"] = f"Find {target.removeprefix('work:')}"
    path.write_text(json.dumps(suite), encoding="utf-8")
    try:
        validate_gold(expanded_tasks(load_suite(path)), corpus)
    except ValueError as exc:
        assert "identifier leaked" in str(exc)
    else:
        raise AssertionError("a target DOI leaked into the external query")


def test_external_answer_metrics_measure_final_multi_target_coverage(tmp_path) -> None:
    corpus = load_corpus(load_suite())
    suite, _path = _external_suite(tmp_path, corpus)
    task = expanded_tasks(suite)[0]
    records = {record["work_id"]: record for record in corpus["records"]}
    targets = task["expect"]["required_ids"]
    response = (
        "；".join(records[target]["title"] for target in targets) + "。仅依据摘要，未查全文。"
    )
    scored = automatic_score(_record(task, response_text=response), task, corpus["records"])
    external = scored["external_answer"]
    assert external["any_required_hit_at_5"] is True
    assert external["all_required_hit_at_5"] is True
    assert external["required_coverage_at_5"] == 1
    assert external["graded_ndcg_at_5"] == 1

    response = records[targets[0]]["title"] + "。仅依据摘要，未查全文。"
    scored = automatic_score(_record(task, response_text=response), task, corpus["records"])
    external = scored["external_answer"]
    assert external["any_required_hit_at_5"] is True
    assert external["all_required_hit_at_5"] is False
    assert external["required_coverage_at_5"] == 0.5
    assert external["graded_ndcg_at_5"] < 1

    gap = next(item for item in expanded_tasks(suite) if item["case_id"] == "external_14")
    scored = automatic_score(_record(gap), gap, corpus["records"])
    assert scored["external_answer"]["graded_ndcg_at_5"] is None
    assert scored["external_answer"]["required_coverage_at_5"] is None


@pytest.mark.parametrize(
    "suite_id",
    [
        "astra-independent-abstract-v2",
        "astra-independent-abstract-v3",
        "astra-independent-abstract-v4",
    ],
)
def test_external_required_ndcg_does_not_reward_background_in_main_ranking(
    tmp_path, suite_id
) -> None:
    corpus = load_corpus(load_suite())
    suite, _path = _external_suite(tmp_path, corpus)
    suite["suite_id"] = suite_id
    task = expanded_tasks(suite)[0]
    target, background = task["expect"]["required_ids"]
    task["expect"]["required_ids"] = [target]
    task["expect"]["background_ids"] = [background]
    records = {record["work_id"]: record for record in corpus["records"]}
    response = (
        f"背景：{records[background]['title']}。主要候选：{records[target]['title']}。"
        "仅依据摘要，未查全文。"
    )
    scored = automatic_score(_record(task, response_text=response), task, corpus["records"])
    external = scored["external_answer"]
    assert external["required_coverage_at_5"] == 1
    assert external["graded_ndcg_at_5"] == 1 / math.log2(3)


def test_retrieval_count_is_not_an_automatic_or_scientific_hard_gate() -> None:
    task = _task()
    target = task["expect"]["required_ids"][0]
    record = _record(
        task,
        retrieval_actions=17,
        visible_output=f"真实摘要候选 {target}，未核查全文。",
        queries=[{"candidate_diagnostics": [{"work_id": target}]}],
    )
    record["automatic"] = automatic_score(record, task, load_corpus(load_suite())["records"])
    assert record["automatic"]["expected_pass"] is True
    assert "within_retrieval_budget" not in record["automatic"]["checks"]
    judgment = {
        "run_case_id": task["run_case_id"],
        "status": "completed",
        "label_mapping": {"A": "product", "B": "kimi_code"},
        "judgment": {
            "dimensions": [
                {
                    "dimension": dimension,
                    "score_a": 2,
                    "score_b": 2,
                    "winner": "tie",
                    "rationale": "resource use is reported separately",
                }
                for dimension in DIMENSIONS
            ],
            "flags_a": [],
            "flags_b": [],
            "summary": "correct answer with additional retrieval",
        },
    }
    summary = summarize([record], [judgment], UsageTotals().as_dict())
    assert record["hard_gate_pass"] is True
    assert summary["systems"]["product"]["hard_gate_pass"] == 1
    assert summary["systems"]["product"]["median_retrieval_actions"] == 17
    assert summary["systems"]["product"]["max_retrieval_actions"] == 17


def test_stability_preserves_query_drift_score_scale_and_semantic_boundaries() -> None:
    def record(repetition, *, system="product", swapped=False, query_count=1, group="group-a"):
        ids = ["work:b", "work:a"] if swapped else ["work:a", "work:b"]
        queries = (
            [
                {
                    "candidate_diagnostics": [
                        {
                            "work_id": work_id,
                            "candidate_score": 90 - index,
                            "score_version": "v1",
                            "score_scale_group": group,
                        }
                        for index, work_id in enumerate(ids)
                    ]
                }
                for _ in range(query_count)
            ]
            if system == "product"
            else []
        )
        return {
            "system": system,
            "case_id": "stable-case",
            "run_case_id": f"stable-case__r{repetition}",
            "repetition": repetition,
            "completed": True,
            "hard_gate_pass": True,
            "response_text": "different text" if repetition == 3 else "same text",
            "tool_calls": [
                {
                    "name": "search_literature",
                    "arguments": {"keywords": ["changed" if swapped else "same"]},
                }
            ],
            "queries": queries,
            "automatic": {
                "external_answer": {
                    "ranked_work_ids": ids,
                    "required_coverage_at_5": 1,
                }
            },
        }

    result = stability_summary([record(1), record(2), record(3, swapped=True, group="group-b")], [])
    case = result["cases"][0]
    assert case["candidate_id_sets_reproducible"] is True
    assert case["agent_queries_reproducible"] is False
    assert case["candidate_order_reproducible"] is False
    assert case["raw_score_reproducibility"] == "incomparable_or_missing"
    assert case["query_comparisons"][0]["maximum_shared_rank_shift"] == 1
    assert case["final_candidate_minimum_pairwise_jaccard"] == 1
    assert case["semantic_review_required"] is True
    assert result["semantic_status"] == "not_independently_reviewed"
    rendered = render_stability_summary(result)
    assert "查询计划一致" in rendered
    assert "有变化" in rendered
    assert "None" not in rendered

    changed_queries = stability_summary([record(1), record(2, query_count=2)], [])["cases"][0]
    assert changed_queries["raw_score_reproducibility"] == "query_count_differs"
    assert changed_queries["candidate_order_reproducible"] is False

    kimi = stability_summary([record(1, system="kimi_code"), record(2, system="kimi_code")], [])[
        "cases"
    ][0]
    assert kimi["candidate_order_reproducible"] is None
    assert kimi["raw_score_reproducibility"] == "not_available"
    assert kimi["final_candidate_order_reproducible"] is True


def test_remote_http_usage_is_unavailable_not_zero() -> None:
    task = _task()
    product = _record(task, usage=UsageTotals().as_dict() | {"status": "unavailable"})
    kimi = _record(task, system="kimi_code")
    corpus = load_corpus(load_suite())["records"]
    for record in (product, kimi):
        record["automatic"] = automatic_score(record, task, corpus)
    summary = summarize([product, kimi], [], UsageTotals().as_dict())
    assert summary["systems"]["product"]["usage_availability"] == "not_exposed_by_http"
    report = render_report(
        summary,
        [product, kimi],
        {"tasks_sha256": "tasks", "implementation_hash": "implementation"},
    )
    assert "用量与成本为不可用，不按零计算" in report


async def test_judge_retry_preserves_critical_flags_and_gives_exact_error(monkeypatch) -> None:
    import tools.quality_benchmark.evaluation as evaluation
    from langchain_core.messages import AIMessage

    from citation_tool.reasons import ModelConfig

    task = _task()
    draft = {
        "dimensions": [
            {"dimension": name, "score_a": 0, "score_b": 2, "winner": "B", "rationale": "test"}
            for name in DIMENSIONS
        ],
        "flags_a": ["fabricated_source"],
        "flags_b": [],
        "flag_evidence_a": [],
        "flag_evidence_b": [],
        "summary": "test",
    }
    invalid = json.dumps(draft)
    fixed = draft | {"flag_evidence_a": ["虚构的文献"]}

    class Judge:
        def __init__(self):
            self.messages = []

        async def ainvoke(self, messages):
            self.messages.append(list(messages))
            return AIMessage(content=invalid if len(self.messages) == 1 else json.dumps(fixed))

    model = Judge()
    monkeypatch.setattr(evaluation, "ChatOpenAI", lambda **kwargs: model)
    records = [
        _record(task, system=system, visible_output="这是虚构的文献。")
        for system in ("product", "kimi_code")
    ]
    judgments, _ = await evaluation.judge_records(
        records,
        [task],
        load_corpus(load_suite())["records"],
        concurrency=1,
        config=ModelConfig(api_key="test-placeholder"),
    )
    assert judgments[0]["status"] == "completed" and judgments[0]["validation_attempts"] == 2
    assert judgments[0]["judgment"]["flags_a"] == ["fabricated_source"]
    correction = model.messages[1]
    assert any(
        isinstance(message, AIMessage) and message.content == invalid for message in correction
    )
    assert "each critical flag requires one verbatim evidence excerpt" in correction[-1].content
    assert "不更改评价标准" in correction[-1].content


def test_judge_receives_actual_unjudged_sources_and_true_corpus_metadata() -> None:
    from tools.quality_benchmark.evaluation import corpus_profile, judge_evidence

    suite = load_suite()
    corpus = load_corpus(suite)
    task = _task()
    gold = {item["work_id"] for item in evaluation_records(task, corpus["records"])}
    extra = next(item for item in corpus["records"] if item["work_id"] not in gold)
    evidence = judge_evidence(task, corpus["records"], [f"补充来源 DOI {extra['doi']}"])
    assert {item["work_id"] for item in evidence} == gold | {extra["work_id"]}
    profile = corpus_profile(corpus["records"], corpus)
    assert profile["record_count"] == 11002
    assert profile["evidence_levels"] == {"abstract_metadata_only": 11002}
    assert profile["retraction_statuses"] == {"unknown": 10952, "none": 48, "corrected": 2}
    assert profile["journal_fields_present"] == 0
    assert profile["core_record_count"] == 50
    assert profile["source_scope"]["governance_scope"] == corpus["governance_scope"]


def test_rescore_reuses_only_a_verifiable_frozen_baseline(tmp_path) -> None:
    results = tmp_path / "results"
    runtime = tmp_path / "runtime"
    source = results / "retest"
    baseline_plan = results / "baseline" / "plan.json"
    baseline_plan.parent.mkdir(parents=True)
    baseline_plan.write_text("{}", encoding="utf8")
    original = runtime / "baseline" / "records" / "case.kimi_code.json"
    original.parent.mkdir(parents=True)
    original.write_text("{}", encoding="utf8")
    plan = {"baseline_reused_from": "baseline", "baseline_plan_sha256": file_hash(baseline_plan)}
    assert (
        quality_cli.original_record_path(source, runtime, plan, original.name, "kimi_code")
        == original
    )
    with pytest.raises(ValueError, match="missing original"):
        quality_cli.original_record_path(source, runtime, plan, "case.product.json", "product")
    baseline_plan.write_text('{"changed":true}', encoding="utf8")
    with pytest.raises(ValueError, match="baseline plan changed"):
        quality_cli.original_record_path(source, runtime, plan, original.name, "kimi_code")
    with pytest.raises(ValueError, match="must not contain a path"):
        quality_cli.original_record_path(source, runtime, plan, "other/file.json", "kimi_code")


def test_development_report_discloses_reused_baseline_and_failed_rankings() -> None:
    task = _task()
    product = _record(
        task,
        split="holdout",
        queries=[
            {
                "candidate_rankings": [{"claim_id": "c", "status": "failed"}],
                "candidate_diagnostics": [
                    {"work_id": "w", "ranking_status": "failed", "reason": {"status": "failed"}}
                ],
            }
        ],
    )
    kimi = _record(task, system="kimi_code", split="holdout")
    corpus = load_corpus(load_suite())["records"]
    for record in (product, kimi):
        record["automatic"] = automatic_score(record, task, corpus)
    summary = summarize([product, kimi], [], UsageTotals().as_dict())
    report = render_report(
        summary,
        [product, kimi],
        {
            "tasks_sha256": "tasks",
            "implementation_hash": "implementation",
            "evidence_status": "development_retest",
            "baseline_reused_from": "frozen-original",
        },
    )
    assert "没有重新运行 Kimi" in report and "frozen-original" in report
    assert "不是新的独立留出" in report and "开发复测（原 holdout）" in report
    assert "查询榜单：0/1 完成；1 次失败" in report
    assert "可排序候选理由：0/1 生成成功" in report
