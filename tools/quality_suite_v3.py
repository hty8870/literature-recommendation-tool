"""Materialize the source-reviewed multi-domain extension without changing v2."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "benchmark/quality"


def main() -> None:
    suite = json.loads((PUBLIC / "tasks-v2.json").read_text(encoding="utf-8"))
    additions = json.loads((PUBLIC / "tasks-v3-additions.json").read_text(encoding="utf-8"))
    if len(additions) != 8:
        raise ValueError("v3 requires eight source-reviewed multi-domain tasks")
    if {task["case_id"] for task in additions} & {task["case_id"] for task in suite["tasks"]}:
        raise ValueError("v3 task IDs must be new")
    corpus_path = ROOT / "data/runtime/quality-corpus/large-corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    known = {record["work_id"] for record in corpus["records"]}
    core = set(suite["core_work_ids"])
    if any(set(task["expect"]["required_ids"]) & core for task in additions):
        raise ValueError("v3 expansion tasks must target records outside the original core")
    for task in additions:
        if not set(task["expect"]["required_ids"]).issubset(known):
            raise ValueError(f"missing source-reviewed target: {task['case_id']}")
    suite.update(
        {
            "suite_id": "abiotech-multidomain-abstract-v3",
            "corpus_path": "data/runtime/quality-corpus/large-corpus.json",
            "tuning_case_ids": [
                *suite["tuning_case_ids"],
                *[task["case_id"] for task in additions],
            ],
            "tasks": [*suite["tasks"], *additions],
        }
    )
    (PUBLIC / "tasks-v3.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"tasks": len(suite["tasks"]), "added": len(additions)}, indent=2))


if __name__ == "__main__":
    main()
