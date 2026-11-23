"""Paired Luna API evaluation of manuscript locations or neighboring paragraphs on QASPER."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from statistics import mean, median

import httpx
from langchain_core.messages import ToolMessage
from tools import qasper_benchmark as qas
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark.data import save_new
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_native_product_retest import (
    NativeModelAudit,
    NativeModelSlots,
    build_product_app,
    collect_model_audit,
)
from tools.parallel_luna import MAX_INFLIGHT, SUBJECT, ObservedLunaClient, ParallelLunaGateway
from tools.qasper_context_seed import fixture as context_fixture
from tools.qasper_context_seed import seed as seed_context
from tools.qasper_scoring import parse_answer, score

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json

BASELINE = qas.BASE / "qasper-runs/qasper-api-luna-v2-20261119"
RUNS = qas.BASE / "qasper-reader-pairs"
ARMS = ("off", "on")
EXPERIMENTS = ("reader", "neighbors", "workspace", "context")
CONTEXT_BINS = {"extractive": 3, "abstractive": 3, "boolean": 2, "unanswerable": 2}
ABSENT_TABLE = frozenset({"qas-21", "qas-36"})
METRICS = (
    "full_evidence",
    "answerability",
    "answer_f1",
    "evidence_f1",
    "same_reference_f1",
)


def fingerprint() -> str:
    return sha256_json(
        {
            "base": qas.code_hash(),
            "runner": file_hash(Path(__file__)),
            "http": file_hash(Path(__file__).with_name("parallel_luna.py")),
            "context_fixture": file_hash(Path(__file__).with_name("qasper_context_seed.py")),
        }
    )


def prepare(root: Path, ceiling: int, experiment: str = "reader") -> dict:
    reference = qas.read_json(BASELINE / "plan.json")
    if (
        reference["archive_sha256"] != qas.ARCHIVE_SHA
        or reference["expected_model"] != MODEL
        or Counter(row["stratum"] for row in reference["cases"]) != qas.QUOTAS
    ):
        raise ValueError("historical source plan is not the agreed QASPER suite")
    root.mkdir(parents=True, exist_ok=False)
    cases = []
    seen: Counter = Counter()
    for case in reference["cases"]:
        cid = case["case"]
        if experiment == "context":
            kind = case["stratum"]
            if cid in ABSENT_TABLE or seen[kind] >= CONTEXT_BINS.get(kind, 0):
                continue
            seen[kind] += 1
        directory = BASELINE / "subject" / cid
        baseline = BASELINE / "runs" / cid / "kimi_code" / "record.json"
        row = qas.read_json(baseline)
        if not qas.admissible(row) or row["model_observed"] != [MODEL]:
            raise ValueError(f"old Kimi comparison is not admissible: {cid}")
        (root / "subject" / cid).mkdir(parents=True)
        files = {
            "paper": (directory / "paper.txt", f"subject/{cid}/paper.txt"),
            "prompt": (directory / "prompt.txt", f"subject/{cid}/prompt.txt"),
            "reference": (BASELINE / "review" / f"{cid}.json", f"review/{cid}.json"),
            "attachment": (directory / "paper.docx", f"subject/{cid}/paper.docx"),
            "kimi": (baseline, f"historical-kimi/{cid}.json"),
        }
        hashes = {}
        for label, (source, relative) in files.items():
            digest = file_hash(source)
            if label in {"paper", "prompt", "reference"} and digest != case[f"{label}_sha256"]:
                raise ValueError(f"frozen {label} mismatch: {cid}")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[relative] = digest
        if experiment == "context":
            relative = f"subject/{cid}/history.json"
            prompt = (root / files["prompt"][1]).read_text(encoding="utf-8")
            save_new(root / relative, context_fixture(prompt))
            hashes[relative] = file_hash(root / relative)
        cases.append({**case, "files": hashes})
    if len({row["paper"] for row in cases}) != len(cases):
        raise ValueError("duplicated paper in paired source")
    probes = reference["probes"]
    if experiment == "context":
        if seen != Counter(CONTEXT_BINS):
            raise ValueError("long-context sample bins are incomplete")
        probes = [
            next(c["case"] for c in cases if c["stratum"] == kind)
            for kind in ("extractive", "unanswerable")
        ]
    plan = {
        "schema": "qasper-reader-pair-v1",
        "created_at": utc_now_iso(),
        "implementation": fingerprint(),
        "source": BASELINE.name,
        "model": MODEL,
        "cases": cases,
        "probes": probes,
        "ceiling": ceiling,
        "primary": "complete reference paragraph set across all answerable allocations",
        "one_change": {
            "reader": "agent_literature_reader (new read_manuscript operations only)",
            "neighbors": "agent_manuscript_find_neighbors; reader on in both arms",
            "workspace": "agent_research_workspace; file tools replace legacy manuscript view",
            "context": "agent_research_context; same file tools in both arms",
        }[experiment],
        "experiment": experiment,
        "historical_kimi": "same material/model, earlier separate CLI sessions; not concurrent",
        "http": "one shared observed Responses client, store=false, no SDK retries",
    }
    if experiment == "neighbors":
        plan["pilot_gate"] = "All four materials verified, at least one same-case scorable pair"
        plan["mechanism"] = (
            "Fully disclosed reference groups; cited annotated passages only from neighbors"
        )
        plan["advance"] = {
            "min_net_complete_groups": 2,
            "min_annotated_neighbor_only_use_cases": 1,
            "max_extra_false_answer_cases": 0,
            "max_extra_undelivered_quote_cases": 0,
            "requires_all_allocated_attempts": True,
            "scope": "development screen for independent validation; never enables a default",
        }
    save_new(root / "plan.json", plan)
    return plan


def validated_plan(root: Path) -> dict:
    plan = qas.read_json(root / "plan.json")
    if fingerprint() != plan["implementation"] or plan["model"] != MODEL:
        raise ValueError("frozen implementation or model changed")
    for case in plan["cases"]:
        for relative, digest in case["files"].items():
            if file_hash(root / relative) != digest:
                raise ValueError(f"frozen artifact mismatch: {relative}")
    return plan


class CaseAudit(NativeModelAudit):
    def __init__(self, directory, lines):
        super().__init__(directory, NativeModelSlots())
        self.lines = lines
        self.visible: set[tuple[str, int, int]] = set()
        self.primary_windows: set[tuple[str, int, int]] = set()
        self.neighbor_windows: set[tuple[str, int, int]] = set()
        self.observations: dict[str, dict] = {}
        self.history_turns: set[int] = set()
        self.bad_spans = 0

    def _observe(self, rows: list[dict], windows: set) -> int:
        count = 0
        for item in rows:
            idx, text = item.get("passage_index"), item.get("text")
            start = item.get("window_start", item.get("start", 0))
            if (
                type(idx) is not int
                or not 0 <= idx < len(self.lines)
                or not isinstance(text, str)
                or type(start) is not int
                or start < 0
                or not text
                or self.lines[idx][start : start + len(text)] != text
            ):
                self.bad_spans += 1
                continue
            end = start + len(text)
            if item.get("window_end", item.get("end", end)) != end:
                self.bad_spans += 1
                continue
            body_start = self.lines[idx].index("] ") + 2
            if end > body_start:
                span = (self.lines[idx][:5], max(0, start - body_start), end - body_start)
                self.visible.add(span)
                windows.add(span)
            count += 1
        return count

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        await super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)
        for group in messages:
            for message in group:
                if (
                    not isinstance(message, ToolMessage)
                    or message.name not in {"read_manuscript", "Read", "Grep"}
                    or message.tool_call_id in self.observations
                ):
                    continue
                try:
                    payload = json.loads(message.content)
                except (ValueError, TypeError):
                    payload = None
                if not isinstance(payload, dict) or payload.get("error"):
                    self.observations[message.tool_call_id] = {"view": "error", "spans": 0}
                    continue
                if message.name in {"Read", "Grep"}:
                    if payload.get("kind") == "conversation_history_not_evidence":
                        if payload.get("lines"):
                            self.history_turns.add(payload["provenance"]["turn_index"])
                        continue
                    if payload.get("kind") != "manuscript":
                        continue
                    projected = [
                        {
                            "passage_index": line["anchor"]["passage_index"],
                            "start": line["anchor"]["start"],
                            "end": line["anchor"]["end"],
                            "text": line["text"],
                        }
                        for line in payload["lines"]
                        if line["text"]
                    ]
                    self.observations[message.tool_call_id] = {
                        "view": message.name,
                        "spans": self._observe(projected, self.primary_windows),
                    }
                    continue
                self.observations[message.tool_call_id] = {
                    "view": payload.get("view") or "passages",
                    "spans": self._observe(
                        payload.get("passages", []) + payload.get("matches", []),
                        self.primary_windows,
                    ),
                    "neighbor_spans": self._observe(
                        payload.get("neighbor_passages", []), self.neighbor_windows
                    ),
                }

    def evidence(self, answer: str):
        value = parse_answer(answer)
        received = []
        if value:
            paragraphs = {line[:5]: line.partition("] ")[2] for line in self.lines}
            for item in value["evidence"]:
                pid, quote = item["passage_id"], item["quote"]
                text = paragraphs.get(pid, "")
                received.append(
                    bool(
                        quote
                        and any(
                            quote in text[start:end]
                            for key, start, end in self.visible
                            if pid == key
                        )
                    )
                )
        return received


async def subject(root, case, arm, config, client, uploads, experiment="reader"):
    cid = case["case"]
    runtime = root / "runs" / cid / arm
    record = runtime / "record.json"
    if record.exists():
        return qas.read_json(record)
    await asyncio.to_thread(runtime.mkdir, parents=True, exist_ok=False)
    material = root / "subject" / cid
    lines = (material / "paper.txt").read_text(encoding="utf-8").splitlines()
    audit = CaseAudit(runtime / "model-calls", lines)
    token = SUBJECT.set(f"{cid}/{arm}")
    started = time.monotonic()
    material_ok = False
    doc_id = None
    result = {}
    error = None
    try:
        settings = replace(
            qas.product_settings(runtime, material),
            agent_literature_reader=experiment != "reader" or arm == "on",
            agent_manuscript_find_neighbors=experiment == "neighbors" and arm == "on",
            agent_research_workspace=experiment == "context"
            or (experiment == "workspace" and arm == "on"),
            agent_research_context=experiment == "context" and arm == "on",
        )
        gateway = ParallelLunaGateway(config, client, audit)
        library = settings.libraries[0]
        app, _, _ = build_product_app(settings, qas.ClosedSource(library), gateway, None)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
            ) as asgi,
        ):
            async with uploads:
                doc_id = await qas.upload(asgi, material / "paper.docx", lines)
                material_ok = True
            route = "/api/v1/agent/runs"
            payload = {
                "input_type": "full_text",
                "manuscript_id": doc_id,
                "library_ids": [library.library_id],
                "message": (material / "prompt.txt").read_text(encoding="utf-8"),
                "prepare_citations": False,
            }
            if experiment == "context":
                history = material / "history.json"
                cid_context = await asyncio.to_thread(
                    seed_context,
                    app.state.store,
                    settings.default_tenant,
                    doc_id,
                    library.library_id,
                    history,
                )
                route = f"/api/v1/conversations/{cid_context}/turns"
                payload = {
                    "message": qas.read_json(history)["followup"],
                    "library_ids": [library.library_id],
                    "prepare_citations": False,
                }
            sent = await qas.fetch_json(asgi, "POST", route, json=payload)
            run_id = sent["run_id"]
            task = app.state.agent_runner._tasks.get(run_id)
            if task is not None:
                await task
            result = await qas.fetch_json(asgi, "GET", f"/api/v1/agent/runs/{run_id}")
    except Exception as exc:  # noqa: BLE001 - preserve failed first attempts
        error = type(exc).__name__
    finally:
        SUBJECT.reset(token)
    models = collect_model_audit(runtime / "model-calls")
    answer = result.get("summary") or ""
    if secret_leak(answer, [config.api_key]):
        answer, error = "", "POTENTIAL_SECRET_WITHHELD"
    error = error or (result.get("error") or {}).get("code")
    body_calls = [row for row in audit.observations.values() if row["spans"] > 0]
    row = {
        "case": cid,
        "arm": arm,
        "run_id": result.get("run_id"),
        "manuscript_id": doc_id,
        "completed": result.get("status") == "completed" and not error and bool(answer),
        "model_valid": models["model_valid"],
        "model_observed": models["served_models"],
        "error": error,
        "material_verified": material_ok,
        "answer": answer,
        "body_calls": body_calls,
        "all_reader_calls": list(audit.observations.values()),
        "bad_spans": audit.bad_spans,
        "visible_passages": len({pid for pid, _, _ in audit.visible}),
        "quotes_in_delivered_windows": audit.evidence(answer),
        "receipt_windows": (
            {"primary": sorted(audit.primary_windows), "neighbors": sorted(audit.neighbor_windows)}
            if experiment != "reader" and models["model_valid"]
            else None
        ),
        "archived_turns_read": sorted(audit.history_turns),
        "history_replay": result.get("context_manifest", {}).get("research_context"),
        "model_calls": models["model_calls"],
        "usage": models["usage"],
        "wall_s": round(time.monotonic() - started, 3),
        "query_count": len(result.get("queries", [])),
    }
    row["admissible"] = bool(
        row["completed"]
        and row["model_valid"]
        and material_ok
        and body_calls
        and not audit.bad_spans
    )
    await asyncio.to_thread(save_new, record, row)
    print(
        json.dumps(
            {
                key: row[key]
                for key in (
                    "case",
                    "arm",
                    "completed",
                    "admissible",
                    "error",
                    "visible_passages",
                    "wall_s",
                )
            }
        ),
        flush=True,
    )
    return row


def attempts(cases):
    for case in cases:
        for arm in ARMS if case["ordinal"] % 2 else ARMS[::-1]:
            yield case, arm


def _metrics(diagnostic: dict, stratum: str):
    answerable = stratum not in {"mixed", "unanswerable"}
    return {
        "full_evidence": int(bool(diagnostic["complete_reference_paragraph_set"]))
        if answerable
        else None,
        "answerability": int(bool(diagnostic["answerability_match"]))
        if stratum != "mixed"
        else None,
        "answer_f1": (diagnostic["reference_token_f1"] or 0) if answerable else None,
        "evidence_f1": (diagnostic["evidence_paragraph_f1"] or 0) if answerable else None,
        "same_reference_f1": (diagnostic["same_reference_min_f1"] or 0) if answerable else None,
    }


def paired(rows, metric):
    values = defaultdict(dict)
    for row in rows:
        if row[metric] is not None:
            values[row["case"]][row["arm"]] = row[metric]
    samples = [v["on"] - v["off"] for v in values.values() if set(ARMS) <= v.keys()]
    if not samples:
        return {"pairs": 0}
    rng = random.Random(20261120)  # noqa: S311 - deterministic statistical resampling
    estimates = sorted(mean(rng.choices(samples, k=len(samples))) for _ in range(10_000))
    return {
        "pairs": len(samples),
        "delta": mean(samples),
        "ci95": [estimates[250], estimates[9749]],
        "better": sum(x > 1e-12 for x in samples),
        "worse": sum(x < -1e-12 for x in samples),
        "tied": sum(abs(x) <= 1e-12 for x in samples),
    }


def _fully_delivered(windows, passages):
    by_passage = defaultdict(list)
    for pid, start, end in windows:
        by_passage[pid].append((start, end))
    full = set()
    for pid, spans in by_passage.items():
        covered = 0
        for start, end in sorted(spans):
            if start > covered:
                break
            covered = max(covered, end)
        if pid in passages and covered >= len(passages[pid]):
            full.add(pid)
    return full


def _neighbor_diagnostic(record, passages, refs, diagnostic):
    windows = record.get("receipt_windows")
    if windows is None:
        return None
    primary, neighbors = windows["primary"], windows["neighbors"]
    core = _fully_delivered(primary, passages)
    full = _fully_delivered(primary + neighbors, passages)
    groups = [set(ref["paragraph_ids"]) for ref in refs if ref["paragraph_ids"]]
    annotated = set().union(*groups)

    def contains(windows, pid, quote):
        return any(p == pid and quote in passages[p][start:end] for p, start, end in windows)

    evidence = (parse_answer(record["answer"]) or {}).get("evidence", [])
    neighbor_citations = (
        sorted(
            {
                item["passage_id"]
                for item, proof in zip(evidence, diagnostic["evidence_items"], strict=True)
                if proof["exact"]
                and contains(neighbors, item["passage_id"], item["quote"])
                and not contains(primary, item["passage_id"], item["quote"])
            }
        )
        if diagnostic["format_valid"]
        else []
    )
    return {
        "neighbor_ids": sorted({pid for pid, _, _ in neighbors}),
        "annotated_full_from_neighbors_only": sorted((full - core) & annotated),
        "reference_group_fully_delivered": any(group <= full for group in groups),
        "reference_group_fully_delivered_by_primary": any(group <= core for group in groups),
        "cited_neighbor_only_ids": neighbor_citations,
        "annotated_neighbor_only_cites": sorted(set(neighbor_citations) & annotated),
    }


def _summarize_neighbors(rows, plan):
    groups = defaultdict(dict)
    for row in rows:
        groups[row["case"]][row["arm"]] = row
    scorable = {
        case
        for case, group in groups.items()
        if all(group[a]["admissible"] and group[a]["diagnostic"]["format_valid"] for a in ARMS)
    }
    compared = [r for r in rows if r["case"] in scorable and r["arm"] in ARMS]
    arms = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        audits = [row["neighbor_diagnostic"] for row in selected if row["neighbor_diagnostic"]]
        arms[arm] = {
            "cases_with_neighbors": sum(bool(a["neighbor_ids"]) for a in audits),
            "cases_with_new_annotated_text": sum(
                bool(a["annotated_full_from_neighbors_only"]) for a in audits
            ),
            "cases_citing_annotated_neighbor_only": sum(
                bool(a["annotated_neighbor_only_cites"]) for a in audits
            ),
            "reference_group_fully_delivered": sum(
                a["reference_group_fully_delivered"] for a in audits
            ),
            "reference_group_fully_delivered_by_primary": sum(
                a["reference_group_fully_delivered_by_primary"] for a in audits
            ),
            "false_answer_cases": sum(
                r["stratum"] == "unanswerable"
                and r["admissible"]
                and r["answer_status"] == "answered"
                for r in selected
            ),
            "undelivered_quote_cases": sum(
                r["admissible"] and False in (r["delivered_quotes"] or []) for r in selected
            ),
            "full_evidence": sum(r["full_evidence"] or 0 for r in selected),
        }
    old, new = arms["off"], arms["on"]
    advance = plan["advance"]
    checks = {
        "all_attempted": all(r["record"] for r in rows if r["arm"] in ARMS),
        "net_complete_groups": new["full_evidence"] - old["full_evidence"]
        >= advance["min_net_complete_groups"],
        "annotated_neighbor_only_use": new["cases_citing_annotated_neighbor_only"]
        >= advance["min_annotated_neighbor_only_use_cases"],
        "no_extra_false_answer_cases": new["false_answer_cases"] - old["false_answer_cases"]
        <= advance["max_extra_false_answer_cases"],
        "no_extra_undelivered_quote_cases": new["undelivered_quote_cases"]
        - old["undelivered_quote_cases"]
        <= advance["max_extra_undelivered_quote_cases"],
    }
    return {
        "arms": arms,
        "scorable_pairs": len(scorable),
        "scorable_paired": {m: paired(compared, m) for m in METRICS},
        "advance": {"passed": all(checks.values()), "checks": checks},
    }


def assess(root, plan):
    experiment = plan.get("experiment", "reader")
    is_neighbor_trial = experiment == "neighbors"
    is_workspace_trial = experiment in {"workspace", "context"}
    arms = ARMS if experiment == "context" else (*ARMS, "historical_kimi")
    rows = []
    for case in plan["cases"]:
        cid = case["case"]
        material = root / "subject" / cid / "paper.txt"
        passages = {
            p[:5]: p.partition("] ")[2] for p in material.read_text(encoding="utf-8").splitlines()
        }
        refs = qas.read_json(root / "review" / f"{cid}.json")
        for arm in arms:
            path = (
                root / "historical-kimi" / f"{cid}.json"
                if arm == "historical_kimi"
                else root / "runs" / cid / arm / "record.json"
            )
            attempted = path.exists()
            if not attempted and not (is_neighbor_trial or is_workspace_trial):
                continue
            row = (
                qas.read_json(path)
                if attempted
                else {
                    "answer": "",
                    "error": "NOT_RUN",
                    "completed": False,
                    "admissible": False,
                }
            )
            admissible = qas.admissible(row) if arm == "historical_kimi" else row["admissible"]
            diagnostic = score(
                row["answer"] if admissible else "",
                stratum=case["stratum"],
                passages=passages,
                references=refs,
            )
            rows.append(
                {
                    "case": cid,
                    "arm": arm,
                    "stratum": case["stratum"],
                    "record": str(path.relative_to(root)) if attempted else None,
                    "admissible": admissible,
                    "completed": row["completed"],
                    "error": row["error"],
                    "wall_s": row.get("wall_s"),
                    "model_calls": row.get("model_calls"),
                    "read_views": dict(Counter(c["view"] for c in row.get("body_calls", []))),
                    "delivered_quotes": row.get("quotes_in_delivered_windows"),
                    "answer_status": (parse_answer(row["answer"]) or {}).get("status"),
                    "diagnostic": diagnostic,
                    **(
                        {
                            "archived_turns_read": row.get("archived_turns_read", []),
                            "history_replay": row.get("history_replay"),
                            "known_absent_table": cid in ABSENT_TABLE,
                            "reference_group_received": (
                                _neighbor_diagnostic(row, passages, refs, diagnostic) or {}
                            ).get("reference_group_fully_delivered"),
                        }
                        if is_workspace_trial
                        else {}
                    ),
                    **_metrics(diagnostic, case["stratum"]),
                    **(
                        {
                            "neighbor_diagnostic": _neighbor_diagnostic(
                                row, passages, refs, diagnostic
                            ),
                        }
                        if is_neighbor_trial
                        else {}
                    ),
                }
            )
    systems = {}
    for arm in arms:
        arm_rows = [row for row in rows if row["arm"] == arm]
        cite_items = [item for row in arm_rows for item in row["diagnostic"]["evidence_items"]]
        metric_values = {m: [row[m] for row in arm_rows if row[m] is not None] for m in METRICS}
        views = Counter()
        for row in arm_rows:
            views.update(row["read_views"])
        systems[arm] = {
            "recorded": sum(row["record"] is not None for row in arm_rows),
            "completed": sum(row["completed"] for row in arm_rows),
            "admissible": sum(row["admissible"] for row in arm_rows),
            "valid_format": sum(row["diagnostic"]["format_valid"] for row in arm_rows),
            "errors": dict(Counter(row["error"] for row in arm_rows if row["error"])),
            "metrics": {
                m: {"n": len(v), "sum": sum(v), "mean": mean(v) if v else None}
                for m, v in metric_values.items()
            },
            "cites": len(cite_items),
            "exact_cites": sum(item["exact"] for item in cite_items),
            "citation_errors": dict(Counter(i["error"] for i in cite_items if i["error"])),
            "body_tool_views": dict(views),
            "used_new_tool_on_cases": sum(
                bool({"find", "verify"} & row["read_views"].keys()) for row in arm_rows
            ),
            "median_wall_s": median(
                [row["wall_s"] for row in arm_rows if row["wall_s"] is not None]
            )
            if any(row["wall_s"] is not None for row in arm_rows)
            else None,
        }
    return {
        "allocated_per_arm": len(plan["cases"]),
        "systems": systems,
        "paired": {metric: paired(rows, metric) for metric in METRICS},
        "rows": rows,
        **({"neighbors": _summarize_neighbors(rows, plan)} if is_neighbor_trial else {}),
    }


async def run(root):
    plan = await asyncio.to_thread(validated_plan, root)
    experiment = plan.get("experiment", "reader")
    config = await asyncio.to_thread(authorized_config)
    uploads = asyncio.Semaphore(2)
    probe_ids = set(plan["probes"])
    probes = [c for c in plan["cases"] if c["case"] in probe_ids]
    remaining = [c for c in plan["cases"] if c["case"] not in probe_ids]
    stop_reason = None
    async with ObservedLunaClient(plan["ceiling"]) as api:
        subjects = asyncio.Semaphore(plan["ceiling"])

        async def dispatch(case, arm):
            async with subjects:
                if api.blocked:
                    return None
                return await subject(root, case, arm, config, api, uploads, experiment)

        first = await asyncio.gather(*(dispatch(c, a) for c, a in attempts(probes)))
        paired_probe = Counter(
            row["case"]
            for row in first
            if row and row["admissible"] and parse_answer(row["answer"])
        )
        can_compare = any(count == len(ARMS) for count in paired_probe.values())
        save_new(
            root / "pilot.json",
            {
                "rows": first,
                "http_peak": api.peak,
                "blocked": api.blocked,
                "paired_probe": can_compare,
            },
        )
        if api.blocked or not all(row and row["material_verified"] for row in first):
            stop_reason = api.blocked or "MATERIAL_PRECHECK_FAILED"
        elif experiment in {"neighbors", "workspace", "context"} and not can_compare:
            stop_reason = "READER_PAIRED_PILOT_UNVERIFIED"
        elif not any(row and row["model_valid"] for row in first):
            stop_reason = "LUNA_NATIVE_UNVERIFIED_IN_PILOT"
        else:
            await api.expand_after_pilot()
            print(
                json.dumps(
                    {
                        "pilot_complete": len(first),
                        "http_peak": api.peak,
                        "next_admission_limit": api.limit,
                    }
                ),
                flush=True,
            )
            await asyncio.gather(*(dispatch(c, a) for c, a in attempts(remaining)))
            stop_reason = api.blocked
        save_new(
            root / "http.json",
            {
                "peak": api.peak,
                "active": api.active,
                "capacity_changes": api.transitions,
                "rows": api.rows,
            },
        )
    await asyncio.to_thread(validated_plan, root)
    result = assess(root, plan)
    result.update(
        stop_reason=stop_reason,
        http_peak=api.peak,
        http_calls=len(api.rows),
        capacity_changes=api.transitions,
        http_status=dict(Counter(str(x["status"]) for x in api.rows)),
        wall_s=round(time.monotonic() - api.started_at, 2),
    )
    save_new(root / "assessment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "assess"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--experiment", choices=EXPERIMENTS, help="prepare only")
    parser.add_argument(
        "--ceiling", type=int, choices=range(1, MAX_INFLIGHT + 1), default=MAX_INFLIGHT
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", args.name):
        parser.error("invalid --name")
    if args.experiment and args.command != "prepare":
        parser.error("--experiment is read from the frozen plan after prepare")
    root = RUNS / args.name
    if args.command == "prepare":
        result = prepare(root, args.ceiling, args.experiment or "reader")
    elif args.command == "run":
        result = asyncio.run(run(root))
    else:
        result = assess(root, qas.read_json(root / "plan.json"))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"cases", "rows"}},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 2 if result.get("stop_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
