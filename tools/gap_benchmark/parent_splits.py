"""Project-local SciFact splits by connected question and reference families."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from tools.benchmark.core import file_hash

from citation_tool.jcs import sha256_json

from .data import RUNTIME, SUITE_ROOT, label, load_data, save_new

ROOT = RUNTIME / "parent-evidence-v1"
MANIFEST = ROOT / "splits.json"
PROTOCOL = SUITE_ROOT / "parent-evidence-v1.md"
SEED = "scifact-parent-family-v1"
COHORTS = {
    "train": {"multi_document": 4, "support": 3, "contradict": 3, "evidence_gap": 2},
    "validation": {"multi_document": 4, "support": 4, "contradict": 4, "evidence_gap": 4},
    "test": {"multi_document": 6, "support": 8, "contradict": 8, "evidence_gap": 6},
    "end_to_end": {"multi_document": 2, "support": 2, "contradict": 2, "evidence_gap": 2},
}


def _order(value: str) -> str:
    return hashlib.sha256(f"{SEED}:{value}".encode()).hexdigest()


def _words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _stratum(row: dict) -> str:
    if len(row["evidence"]) >= 2:
        return "multi_document"
    direction = label(row)
    return "evidence_gap" if direction == "NEI" else direction.lower()


def family_partitions(
    public_train: list[dict], exposed_dev: list[dict], records: list[dict]
) -> list[dict]:
    all_claims = sorted([*public_train, *exposed_dev], key=lambda row: row["id"])
    parents = {row["id"]: row["id"] for row in all_claims}
    if len(parents) != len(all_claims):
        raise ValueError("duplicate upstream claim ID")

    def root(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def connect(first: int, second: int) -> None:
        a, b = sorted((root(first), root(second)))
        parents[b] = a

    document_keys = {}
    for record in records:
        doc = str(record["doc_id"])
        title = _words(record["title"])
        abstract = _words(record["abstract"])
        keys = [("id", doc)]
        if len(title) >= 4:
            keys.append(("title", title))
        if len(abstract) >= 20:
            keys.append(("abstract", abstract))
        document_keys[doc] = keys
    seen = {}
    shingles = []
    for row in all_claims:
        claim = row["id"]
        words = _words(row["claim"])
        keys = [("claim", words)]
        for doc in {*map(str, row["cited_doc_ids"]), *row["evidence"]}:
            keys.extend(document_keys[doc])
        for key in keys:
            previous = seen.setdefault(key, claim)
            connect(claim, previous)
        bigrams = set(zip(words, words[1:], strict=False))
        if len(bigrams) >= 5:
            for other, other_bigrams in shingles:
                if (
                    abs(len(bigrams) - len(other_bigrams))
                    > max(len(bigrams), len(other_bigrams)) / 5
                ):
                    continue
                overlap = len(bigrams & other_bigrams)
                if 5 * overlap >= 4 * (len(bigrams) + len(other_bigrams) - overlap):
                    connect(claim, other)
            shingles.append((claim, bigrams))

    groups = defaultdict(list)
    for row in all_claims:
        groups[root(row["id"])].append(row)
    exposed = {row["id"] for row in exposed_dev}
    clean = defaultdict(list)
    assignments = {}
    for family, group in groups.items():
        if any(row["id"] in exposed for row in group):
            assignments[family] = "train"
        else:
            labels = {_stratum(row) for row in group}
            main = next(
                kind
                for kind in ("multi_document", "support", "contradict", "evidence_gap")
                if kind in labels
            )
            clean[main].append(family)
    multi_validation = COHORTS["validation"]["multi_document"]
    multi_test = COHORTS["test"]["multi_document"] + COHORTS["end_to_end"]["multi_document"]
    for stratum, families in clean.items():
        for index, family in enumerate(sorted(families, key=lambda f: _order(f"family:{f}"))):
            if stratum == "multi_document":
                validation = index < multi_validation
                test = multi_validation <= index < multi_validation + multi_test
            else:
                validation = index % 20 in {1, 8, 15}
                test = index % 20 in {0, 4, 9, 13, 17}
            assignments[family] = "validation" if validation else "test" if test else "train"
    return [
        {
            "claim_id": row["id"],
            "case_id": f"sf-{row['id']}",
            "family": f"sf-family-{root(row['id'])}",
            "split": assignments[root(row["id"])],
            "stratum": _stratum(row),
            "upstream_split": "dev" if row["id"] in exposed else "train",
        }
        for row in all_claims
    ]


def select_cohorts(members: list[dict], seen_case_ids: set[str]) -> dict[str, list[str]]:
    chosen = {}
    used = set()
    for cohort, quotas in COHORTS.items():
        fold = "test" if cohort == "end_to_end" else cohort
        selected = []
        for stratum, quota in quotas.items():
            candidates = sorted(
                (
                    row
                    for row in members
                    if row["split"] == fold
                    and row["stratum"] == stratum
                    and (fold != "train" or row["case_id"] in seen_case_ids)
                ),
                key=lambda row: _order(f"{cohort}:{row['case_id']}"),
            )
            count = 0
            for row in candidates:
                if row["family"] in used:
                    continue
                selected.append(row["case_id"])
                used.add(row["family"])
                count += 1
                if count == quota:
                    break
            if count != quota:
                raise ValueError(f"only {count}/{quota} disjoint families for {cohort}/{stratum}")
        chosen[cohort] = sorted(selected, key=lambda case: _order(f"order:{cohort}:{case}"))
    return chosen


def load_sources() -> tuple[dict, dict, dict[int, dict], bytes]:
    suite, corpus = load_data()
    archive = RUNTIME / "source/data.tar.gz"
    if file_hash(archive) != suite["source"]["archive_sha256"]:
        raise ValueError("original SciFact archive hash differs")
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember("data/claims_train.jsonl")
        if not member.isfile() or not 0 < member.size < 1_000_000:
            raise ValueError("invalid SciFact train member")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError("SciFact train member unreadable")
        data = stream.read()
    train = [json.loads(line) for line in data.splitlines()]
    dev = suite["all_claims"]
    if len(train) != 809 or len(dev) != 300:
        raise ValueError("unexpected count of labeled SciFact claims")
    return suite, corpus, {row["id"]: row for row in [*train, *dev]}, data


def prepare_splits() -> dict:
    suite, corpus, claims, train_bytes = load_sources()
    dev = suite["all_claims"]
    exposed = {row["id"] for row in dev}
    rows = family_partitions(
        [row for claim, row in claims.items() if claim not in exposed], dev, corpus["records"]
    )
    cohorts = select_cohorts(rows, {row["case_id"] for row in suite["tasks"]})
    manifest = {
        "schema": "scifact-parent-family-split-v1",
        "seed": SEED,
        "protocol_sha256": file_hash(PROTOCOL),
        "splitter_sha256": file_hash(Path(__file__)),
        "corpus_sha256": corpus["corpus_sha256"],
        "source_tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "source_train_member_sha256": hashlib.sha256(train_bytes).hexdigest(),
        "counts": dict(Counter(row["split"] for row in rows)),
        "family_counts": {
            part: len({row["family"] for row in rows if row["split"] == part})
            for part in ("train", "validation", "test")
        },
        "cohorts": cohorts,
        "members": rows,
    }
    if MANIFEST.exists():
        if json.loads(MANIFEST.read_text(encoding="utf-8")) != manifest:
            raise ValueError("existing split manifest differs")
    else:
        save_new(MANIFEST, manifest)
    return manifest


def load_splits() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        manifest["protocol_sha256"],
        manifest["splitter_sha256"],
        manifest["source_tasks_sha256"],
    ) != (file_hash(PROTOCOL), file_hash(Path(__file__)), file_hash(SUITE_ROOT / "tasks.json")):
        raise ValueError("split inputs changed; do not silently rewrite the allocation")
    return manifest


def summary(manifest: dict) -> dict:
    return {
        "split_sha256": sha256_json(manifest),
        "counts": manifest["counts"],
        "family_counts": manifest["family_counts"],
        "cohort_counts": {name: len(items) for name, items in manifest["cohorts"].items()},
    }


if __name__ == "__main__":
    print(json.dumps(summary(prepare_splits()), indent=2))
