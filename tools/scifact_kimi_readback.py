"""Audit literal corpus evidence in fresh SciFact Kimi tool results, not reasoning traces."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from tools.benchmark.core import ROOT, file_hash
from tools.gap_benchmark.data import save_new
from tools.gap_benchmark.metrics import score_record
from tools.luna_gap_retest import prepare_run
from tools.quality_benchmark.core import project_skill_isolation_valid


def appeared(output: str, value: str) -> bool:
    return bool(value) and any(
        part in output
        for part in (
            value,
            json.dumps(value, ensure_ascii=False)[1:-1],
            json.dumps(value, ensure_ascii=True)[1:-1],
        )
    )


def inspect(path: Path, targets: dict, answer_quotes: dict, by_id: dict) -> dict:
    sentence_hits = {doc: set() for doc in targets}
    quote_hits = set()
    visible = {doc: [] for doc in set(targets) | set(answer_quotes)}
    steps = []
    names = {}
    requests = []
    with ZipFile(path) as archive:
        wire = [
            item for item in archive.infolist() if item.filename.endswith("agents/main/wire.jsonl")
        ]
        if len(wire) != 1 or wire[0].file_size > 100_000_000:
            raise ValueError("missing or oversized fresh main-session trace")
        with archive.open(wire[0]) as stream:
            for line in stream:
                item = json.loads(line)
                if item.get("type") == "llm.request":
                    requests.append(
                        {
                            k: item.get(k)
                            for k in (
                                "model",
                                "thinkingEffort",
                                "messageCount",
                                "toolsHash",
                                "turnStep",
                                "time",
                            )
                        }
                    )
                if item.get("type") != "context.append_loop_event":
                    continue
                event = item.get("event") or {}
                event_type = event.get("type")
                identity = event.get("toolCallId")
                if event_type == "tool.call":
                    names[identity] = event.get("name")
                    continue
                if event_type != "tool.result":
                    continue
                result = event.get("result") or {}
                output = result.get("output")
                tool = names.get(identity)
                steps.append(
                    {
                        "tool": tool,
                        "error": bool(result.get("isError")),
                        "chars": len(output) if isinstance(output, str) else 0,
                    }
                )
                if tool not in {"Read", "Grep", "Bash"} or not isinstance(output, str):
                    continue
                for doc, sightings in visible.items():
                    source = by_id.get(doc)
                    if not source:
                        continue
                    id_seen = bool(re.search(rf"(?<!\d){re.escape(doc)}(?!\d)", output))
                    if not id_seen and not appeared(output, source["title"]):
                        continue
                    if appeared(output, answer_quotes.get(doc, "")):
                        quote_hits.add(doc)
                    matches = {
                        i
                        for rationale in targets.get(doc, [])
                        for i in rationale["sentences"]
                        if appeared(output, source["abstract_sentences"][i])
                    }
                    if doc in sentence_hits:
                        sentence_hits[doc].update(matches)
                    if matches or appeared(output, answer_quotes.get(doc, "")):
                        sightings.append(len(steps))
    return {
        "requests": requests,
        "tools": steps,
        "gold": {
            doc: {
                "observed_full_set": any(set(r["sentences"]) <= sentence_hits[doc] for r in sets),
                "observed_sentence_ids": sorted(sentence_hits[doc]),
                "first_matching_tool_result": min(visible[doc], default=None),
            }
            for doc, sets in targets.items()
        },
        "observed_final_quote_docs": sorted(quote_hits),
    }


def audit(name: str) -> dict:
    root, suite, corpus = prepare_run(name)
    by_id = {r["doc_id"]: r for r in corpus["records"]}
    rows = []
    for task in suite["tasks"]:
        case = task["case_id"]
        record_path = root / "kimi" / f"{case}.json"
        archive = ROOT / "data/runtime/kimi-evaluation" / f"{name}-{case}" / "session.zip"
        if not record_path.is_file() or not archive.is_file():
            continue
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not project_skill_isolation_valid(record) or not record.get("session_id"):
            raise ValueError("session not isolated from project's product skill")
        assessment = score_record(record, task, corpus)["assessment"]
        quotes = {p["doc_id"]: p["quote"] for p in assessment["papers"] if p["exact_quote"]}
        rows.append(
            {
                "case": case,
                "archive_sha256": file_hash(archive),
                **inspect(archive, task["expect"]["evidence"], quotes, by_id),
            }
        )
    return {
        "audited": len(rows),
        "tools": dict(Counter(t["tool"] for row in rows for t in row["tools"])),
        "complete_gold_sets_observed": sum(
            value["observed_full_set"] for row in rows for value in row["gold"].values()
        ),
        "gold_documents": sum(len(row["gold"]) for row in rows),
        "boundary": "Literal text with same-output ID/title; no match does not prove unread.",
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.name)
    save_new(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
