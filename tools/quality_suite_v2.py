"""Versioned, source-reviewed v2 tasks; run explicitly to materialize the suite."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "benchmark/quality"


def main() -> None:
    suite = json.loads((PUBLIC / "tasks.json").read_text(encoding="utf-8"))
    additions = json.loads((PUBLIC / "tasks-v2-additions.json").read_text(encoding="utf-8"))
    extended = json.loads(
        (ROOT / "data/runtime/quality-corpus/extended-corpus.json").read_text(encoding="utf-8")
    )
    if len(additions) != 12:
        raise ValueError("v2 requires twelve source-reviewed additional tasks")
    old_holdout = [
        task["case_id"]
        for task in suite["tasks"]
        if task["case_id"] not in suite["tuning_case_ids"]
    ]
    new_tuning = [task["case_id"] for task in additions[:4]]
    development_ids = [case_id for case_id in old_holdout if case_id != "out_of_domain_zero"]
    suite.update(
        {
            "suite_id": "abiotech-abstract-ood-v2",
            "corpus_path": "data/runtime/quality-corpus/extended-corpus.json",
            "core_work_ids": extended["core_work_ids"],
            "retriever": "hybrid",
            "dense_threshold": 0.80,
            "development_case_ids": development_ids,
            "tuning_case_ids": [*suite["tuning_case_ids"], *new_tuning, "out_of_domain_zero"],
            "tasks": [*suite["tasks"], *additions],
        }
    )
    (PUBLIC / "tasks-v2.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "tasks": len(suite["tasks"]),
                "tuning": len(suite["tuning_case_ids"]),
                "development": len(development_ids),
                "holdout": len(additions[4:]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
