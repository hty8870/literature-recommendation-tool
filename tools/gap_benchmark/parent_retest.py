"""Run a frozen, family-isolated comparison of parent Agent evidence presentations."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tools import luna_native_product_retest as native
from tools import luna_product_retest as product
from tools.benchmark.core import ROOT, file_hash, secret_leak
from tools.luna_credentials import MODEL, authorized_config

from citation_tool.agent import _message_text, _system_prompt, _user_prompt, final_answer_text
from citation_tool.agent_observations import AbstractEvidenceMode, abstract_evidence_observation
from citation_tool.candidate_pool import source_record
from citation_tool.entities import CandidateDiagnostic, SearchCreate
from citation_tool.evidence import build_recommendation
from citation_tool.jcs import sha256_json
from citation_tool.search import _build_claim

from . import parent_metrics, parent_splits
from .data import OUTPUT_INSTRUCTION, SUITE_ROOT, save_new

Trial = Literal["observation", "reasoning", "decision"]
Arm = Literal["legacy", "separate", "scoped", "high", "rubric"]
TRIAL_ARMS: dict[Trial, tuple[Arm, ...]] = {
    "observation": ("legacy", "separate", "scoped"),
    "reasoning": ("legacy", "high"),
    "decision": ("legacy", "rubric"),
}
PROTOCOLS = {
    "observation": parent_splits.PROTOCOL,
    "reasoning": SUITE_ROOT / "parent-reasoning-v1.md",
    "decision": SUITE_ROOT / "parent-decision-v1.md",
}
DECISION_VALIDATION = {"contradict": 6, "support": 6, "evidence_gap": 4}
NOW = "2026-01-01T00:00:00Z"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frozen(path: Path, value: dict) -> None:
    if path.exists():
        if _read(path) != value:
            raise ValueError(f"frozen {path.name} differs; use a new run name")
    else:
        save_new(path, value)


def _contract(manifest: dict, trial: Trial = "observation") -> dict:
    code = [
        *sorted((ROOT / "src/citation_tool").glob("*.py")),
        *sorted((ROOT / "tools/gap_benchmark").glob("*.py")),
        ROOT / "tools/luna_native_product_retest.py",
        ROOT / "tools/luna_product_retest.py",
        ROOT / "tools/luna_credentials.py",
        ROOT / ".agents/skills/recommend-literature/SKILL.md",
        ROOT / "uv.lock",
    ]
    return {
        "schema": "scifact-parent-evidence-ab-v1",
        "split_sha256": sha256_json(manifest),
        "code_sha256": sha256_json({str(p.relative_to(ROOT)): file_hash(p) for p in code}),
        "model": MODEL,
        "transport": native.TRANSPORT,
        "api_host": "api.openai.com",
        "store": False,
        "trial": trial,
        "arms": list(TRIAL_ARMS[trial]),
        "trial_protocol_sha256": file_hash(PROTOCOLS[trial]),
        "reasoning_effort": "parent only: default vs high" if trial == "reasoning" else "default",
        "replay_format": "free_text" if trial == "observation" else "json_object",
        **(
            {
                "replay_delivery": "responses-final-answer-v1",
                "replay_mode": "serial",
                "cohorts": _cohorts(manifest, trial),
            }
            if trial == "decision"
            else {}
        ),
        "evidence_view_v1_enabled": False,
        "retriever": "production BM25/E5/RRF, original claim, production candidate window",
        "max_inflight": 2,
    }


def _cohorts(manifest: dict, trial: Trial) -> dict[str, list[str]]:
    cohorts = dict(manifest["cohorts"])
    if trial != "decision":
        return cohorts
    exposed = set(cohorts["validation"])
    used = {row["family"] for row in manifest["members"] if row["case_id"] in exposed}
    selected = []
    for label, count in DECISION_VALIDATION.items():
        rows = sorted(
            (
                row
                for row in manifest["members"]
                if row["split"] == "validation" and row["stratum"] == label
            ),
            key=lambda row: parent_splits._order(f"decision-v1:{row['case_id']}"),
        )
        chosen = []
        for row in rows:
            if row["family"] in used:
                continue
            used.add(row["family"])
            chosen.append(row["case_id"])
            if len(chosen) == count:
                break
        if len(chosen) != count:
            raise ValueError(f"not enough fresh validation families for {label}")
        selected.extend(chosen)
    cohorts["validation"] = sorted(
        selected, key=lambda case: parent_splits._order(f"decision-v1-order:{case}")
    )
    return cohorts


def _public_task(member: dict, claim_text: str) -> dict:
    case = member["case_id"]
    return {
        "case_id": case,
        "run_case_id": case,
        "repetition": 1,
        "slice": member["stratum"],
        "family": member["family"],
        "request": {"input_type": "abstract", "abstract": claim_text},
        "baseline_query": f"{OUTPUT_INSTRUCTION}\n\n待核查论断：{claim_text}",
        "retriever": "hybrid",
        "dense_threshold": 0.80,
    }


def _pool(task: dict, adapter) -> dict:
    case = task["case_id"]
    claim, _ = _build_claim("parent-replay", case, SearchCreate(**task["request"]), NOW, None)
    recalled = adapter.search(claim)
    if adapter.model_versions.get("dense_status") != "available":
        raise ValueError("frozen replay requires the prebuilt E5 index and dense queries")
    rows = []
    for candidate in recalled:
        source = source_record(candidate)
        rec = build_recommendation(
            tenant_id=claim.tenant_id, query_id=claim.query_id, claim=claim, candidate=candidate
        )
        diagnostic = CandidateDiagnostic(
            result_id=rec.result_id,
            work_id=candidate.passage.work_id,
            paper_id=source.paper_id,
            claim_id=claim.claim_id,
            title=source.title,
            year=source.year,
            doi=source.doi,
            authors=source.authors,
            library_ids=[source.library_id],
            publication_status=source.publication_status,
            relationship="insufficient",
            scope_match=rec.evidence[0].scope_match,
            rejection_reasons=sorted({*rec.rejection_reasons, "abstract_metadata_only"}),
            evidence_excerpt=source.abstract,
            source_uri=source.source_uri,
            source_records=[source],
            ranking_status="disabled",
            quality_note="真实题录与摘要候选，尚无全文证据或引用资格。",
        )
        rows.append(diagnostic.model_dump(mode="json"))
    return {
        "query_id": case,
        "simulation": False,
        "results": [],
        "candidate_diagnostics": rows,
        "diagnostic_note": (
            "这是固定池回放，按真实检索顺序提供摘要；没有 LLM 榜单、推荐理由或全文核验。"
            "候选可用于发现，但不可作为已核验的原文引用。"
        ),
    }


def _supplied(queries: list[dict]) -> set[str]:
    return {
        item["work_id"].removeprefix("work:scifact:")
        for query in queries
        for item in query["candidate_diagnostics"]
    }


def _settings(path: Path, task: dict, arm: AbstractEvidenceMode = "legacy"):
    return replace(product.product_settings(path, task), agent_abstract_evidence_mode=arm)


def _replay_request(task: dict, observation: dict, path: Path, arm: Arm) -> list:
    request = SearchCreate(**task["request"])
    mode = _observation_mode(arm)
    settings = replace(
        _settings(path, task, mode), agent_evidence_decision_guidance=arm == "rubric"
    )
    service = SimpleNamespace(settings=settings, adapter_mode="metadata")
    observation = abstract_evidence_observation(observation, mode)
    return [
        SystemMessage(_system_prompt(request, service, reason_model_configured=False)),
        HumanMessage(
            _user_prompt(request, user_message=task["baseline_query"], extra_blocks=[observation])
        ),
    ]


def _observation_mode(arm: Arm) -> AbstractEvidenceMode:
    return "legacy" if arm in {"high", "rubric"} else arm


def _set_parent_effort(gateway: native.NativeLunaGateway, arm: Arm) -> None:
    if arm == "high":
        model = gateway.agent_model()
        if not isinstance(model, ChatOpenAI) or not model.use_responses_api or model.reasoning:
            raise ValueError("high effort requires a fresh native Responses parent model")
        model.reasoning = {"effort": "high"}


async def _replay(task: dict, observation: dict, path: Path, arm: Arm, config, trial: Trial):
    audit = native.NativeModelAudit(path / "model-calls", native.NativeModelSlots())
    gateway = native.NativeLunaGateway(config, audit)
    _set_parent_effort(gateway, arm)
    request = _replay_request(task, observation, path, arm)
    parameters = {"response_format": {"type": "json_object"}} if trial != "observation" else {}
    request_sha256 = sha256_json(
        {
            "messages": [item.content for item in request],
            "format": parameters,
            "reasoning_effort": "high" if arm == "high" else "default",
        }
    )
    text = ""
    error = None
    delivery = None
    start = time.monotonic()
    try:
        async with asyncio.timeout(900):
            response = await gateway.agent_model().ainvoke(request, **parameters)
            if trial == "decision":
                from .metrics import parse_answer

                text = final_answer_text(response) or ""
                blocks = response.content if isinstance(response.content, list) else []
                delivery = {
                    "final_answer_blocks": sum(
                        isinstance(b, dict) and b.get("phase") == "final_answer" for b in blocks
                    ),
                    "commentary_blocks": sum(
                        isinstance(b, dict) and b.get("phase") == "commentary" for b in blocks
                    ),
                    "legacy_join_valid": parse_answer(_message_text(response.content) or "")[0]
                    is not None,
                }
                if not text:
                    error = "NoFinalAnswer"
            else:
                text = _message_text(response.content) or ""
    except Exception as exc:
        error = type(exc).__name__
    finally:
        await gateway.aclose()
    calls = native.collect_model_audit(path / "model-calls")
    return {
        "completed": error is None and bool(text),
        "model_valid": calls["model_valid"],
        "model_observed": calls["served_models"],
        "native_response_ids": calls["response_ids"],
        "usage": calls["usage"],
        "wall_time_ms": round((time.monotonic() - start) * 1000),
        "request_sha256": request_sha256,
        "response_text": text,
        "supplied": sorted(_supplied([observation])),
        "secret_leak": secret_leak(text, []),
        "error": error,
        **({"delivery": delivery} if trial == "decision" else {}),
    }


async def _end_to_end(task, path, arm, config, root, corpus, index, fingerprint):
    def builder(settings, adapter, gateway, callback):
        _set_parent_effort(gateway, arm)
        return native.build_product_app(
            replace(
                settings,
                agent_abstract_evidence_mode=_observation_mode(arm),
                agent_evidence_decision_guidance=arm == "rubric",
            ),
            adapter,
            gateway,
            callback,
        )

    record = await native.run_case(
        task,
        root=root,
        runtime=path,
        corpus=corpus,
        index=index,
        implementation_hash=fingerprint,
        config=config,
        slots=native.NativeModelSlots(),
        app_builder=builder,
    )
    visible_queries = {c["query_id"] for c in record["tool_calls"] if c["status"] == "completed"}
    record["supplied"] = sorted(
        _supplied([q for q in record["queries"] if q["query_id"] in visible_queries])
    )
    return record


def _cohort_arms(root: Path, cohort: str, plan_hash: str, trial: Trial) -> tuple[Arm, ...]:
    arms = TRIAL_ARMS[trial]
    if cohort == "train" or (cohort == "validation" and trial == "reasoning"):
        return arms
    previous = "train" if cohort == "validation" else "validation" if cohort == "test" else "test"
    path = root / previous / "assessment.json"
    if not path.is_file():
        raise ValueError(f"finish {previous} first")
    assessment = _read(path)
    if assessment["plan_sha256"] != plan_hash or assessment["cohort"] != previous:
        raise ValueError(f"{previous} result belongs to another contract")
    if cohort == "validation":
        return arms
    selection = assessment.get("selected_arm")
    if selection not in arms[1:] or (cohort == "end_to_end" and not assessment["test_gate_passed"]):
        raise ValueError("preregistered quality gate stayed closed; leave this cohort untouched")
    return ("legacy", selection)


def _frozen_validation_pool(root: Path, task: dict) -> dict:
    case = task["case_id"]
    stage = root / "citation-vs-direction-ab-v1" / "validation"
    subject = _read(stage / "subject" / f"{case}.json")
    original = _read(stage / "records" / case / "legacy.json")
    if (
        sha256_json(subject) != original["subject_sha256"]
        or subject["request"] != task["request"]
        or subject["user_message"] != task["baseline_query"]
    ):
        raise ValueError("validation subject no longer matches the original replay")
    return subject["observation"]


async def run(name: str, cohort: str, *, resume: bool, trial: Trial = "observation") -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError("invalid experiment name")
    manifest = parent_splits.load_splits()
    contract = _contract(manifest, trial)
    root = parent_splits.ROOT / "runs" / name
    _frozen(root / "plan.json", contract)
    plan_hash = sha256_json(contract)
    arms = _cohort_arms(root, cohort, plan_hash, trial)
    stage = root / cohort
    if (stage / "assessment.json").exists() and not resume:
        raise FileExistsError("assessment is already frozen")
    _, corpus, claims, train_bytes = parent_splits.load_sources()
    if (
        contract["split_sha256"] != sha256_json(manifest)
        or corpus["corpus_sha256"] != manifest["corpus_sha256"]
        or sha256(train_bytes).hexdigest() != manifest["source_train_member_sha256"]
    ):
        raise ValueError("corpus or labels no longer match the frozen split")
    by_id = {row["doc_id"]: row for row in corpus["records"]}
    members = {row["case_id"]: row for row in manifest["members"]}
    cases = _cohorts(manifest, trial)[cohort]
    subjects = [_public_task(members[c], claims[members[c]["claim_id"]]["claim"]) for c in cases]
    settings = _settings(stage / "preflight", subjects[0])
    index = await asyncio.to_thread(product.require_prebuilt_index, corpus, settings)
    reuse_validation = trial == "reasoning" and cohort == "validation"
    adapter = (
        await asyncio.to_thread(product.build_product_adapter, settings, corpus, index, subjects[0])
        if cohort != "end_to_end" and not reuse_validation
        else None
    )
    config = None
    response_ids = set()
    rows = []

    async def arm_result(task, observation, arm, subject_sha):
        case_id = task["case_id"]
        dest = stage / "records" / case_id / f"{arm}.json"
        header = {
            "plan_sha256": plan_hash,
            "cohort": cohort,
            "arm": arm,
            "case_id": case_id,
            "subject_sha256": subject_sha,
        }
        if dest.exists():
            if not resume:
                raise FileExistsError("existing outputs require --resume")
            record = _read(dest)
            if any(record[key] != value for key, value in header.items()):
                raise ValueError("existing record does not match frozen case")
        else:
            path = await asyncio.to_thread(product.new_attempt_dir, stage, f"{case_id}-{arm}")
            if observation is not None:
                record = await _replay(task, observation, path, arm, config, trial)
            else:
                record = await _end_to_end(task, path, arm, config, root, corpus, index, plan_hash)
            record = {**record, **header}
            await asyncio.to_thread(save_new, dest, record)
            print(
                json.dumps(
                    {
                        **header,
                        "model_valid": record["model_valid"],
                        "completed": record["completed"],
                        "error": record["error"],
                    }
                ),
                flush=True,
            )
        error = record.get("error")
        if isinstance(error, dict) and error.get("code") == "NATIVE_MODEL_CLEANUP_FAILED":
            raise ValueError("native model work was not released; stopping")
        return record

    for position, task in enumerate(subjects):
        case = task["case_id"]
        member = members[case]
        if reuse_validation:
            observation = _frozen_validation_pool(root.parent, task)
        else:
            observation = await asyncio.to_thread(_pool, task, adapter) if adapter else None
        subject = {
            "request": task["request"],
            "user_message": task["baseline_query"],
            "observation": observation,
        }
        await asyncio.to_thread(_frozen, stage / "subject" / f"{case}.json", subject)
        subject_sha = sha256_json(subject)
        start = position % len(arms)
        ordered = arms[start:] + arms[:start]
        paths = [stage / "records" / case / f"{arm}.json" for arm in ordered]
        if not resume and any(p.exists() for p in paths):
            raise FileExistsError("existing outputs require --resume")
        if config is None and any(not p.exists() for p in paths):
            config = await asyncio.to_thread(authorized_config)
        if trial == "reasoning" and observation is not None:
            records = await asyncio.gather(
                *(arm_result(task, observation, arm, subject_sha) for arm in ordered)
            )
        else:
            records = [await arm_result(task, observation, arm, subject_sha) for arm in ordered]
        output = {
            "case_id": case,
            "family": task["family"],
            "stratum": member["stratum"],
            "arms": {},
        }
        for arm, record in zip(ordered, records, strict=True):
            observed_ids = record["native_response_ids"]
            if response_ids.intersection(observed_ids):
                raise ValueError("reused native response ID")
            response_ids.update(observed_ids)
            output["arms"][arm] = parent_metrics.score(
                record, claims[member["claim_id"]]["evidence"], by_id, set(record["supplied"])
            )
        rows.append(output)
    if _contract(manifest, trial) != contract:
        raise ValueError("implementation changed during the run; keeping observations unscored")
    report = {"cohort": cohort, "plan_sha256": plan_hash, **parent_metrics.compare(rows, arms)}
    if cohort == "validation":
        report["selected_arm"] = parent_metrics.select_variant(report)
    elif cohort == "test":
        report["selected_arm"] = arms[1]
        report["test_gate_passed"] = parent_metrics.test_passed(report, arms[1])
    elif cohort == "end_to_end":
        winner = report["paired"][arms[1]]
        report["eligible_for_rollout_consideration"] = (
            winner["delta"] >= -1e-9 and not winner["guardrail_blockers"]
        )
    _frozen(stage / "assessment.json", report)
    return {k: v for k, v in report.items() if k not in {"strata", "cases"}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", choices=parent_splits.COHORTS)
    parser.add_argument("--name", required=True)
    parser.add_argument("--trial", choices=TRIAL_ARMS, default="observation")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(run(args.name, args.cohort, resume=args.resume, trial=args.trial))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
