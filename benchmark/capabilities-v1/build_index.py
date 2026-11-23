"""Rebuild the public diagnostic inventory from pinned local files. No evaluation or network."""

import argparse
import csv
import hashlib
import io
import json
import tarfile
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

LIT = "full-00000-of-00001.parquet"
QAS = "qasper-test-and-evaluator-v0.3.tgz"
PROMPTS = "prompts_merged.csv"
ANNOTATIONS = "annotations_merged.csv"
SPLIT = "ev2_test_article_ids.txt"
PINS = {
    LIT: "38cdbe4a6b7a7f5776d08055bfbf6e4511000aa2a523866592b4bb273388b914",
    QAS: "72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72",
    PROMPTS: "0d70d9e1e78d113fdbbd4919310eab7768d6a328368f7d8f9d5a3b48f99c25d3",
    ANNOTATIONS: "a5dd1a3583e5105a0f77d3e1c695008879859425c9a93dfbc07054ef462e0255",
    SPLIT: "3010f0df3b322fe9d8b4bbc2b9c11a57e1f3586f70b06a5402255e62c310ba9e",
}
EXPECTED = {"lit": 597, "qas": 1451, "ei": 287}


def checked(directory: Path, name: str) -> bytes:
    data = (directory / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != PINS[name]:
        raise ValueError(f"Upstream file differs from the registered version: {name}")
    return data


def record(case: str, family: str, stratum: str, *question: str):
    if not all(s.strip() for s in question):
        raise ValueError(f"Missing request data for {case}")
    payload = json.dumps(question, ensure_ascii=False, separators=(",", ":")).encode()
    audit = {
        "id": case,
        "family_hint": family,
        "stratum": stratum,
        "input_sha256": hashlib.sha256(payload).hexdigest(),
    }
    request = {"key": hashlib.sha256(f"capabilities-v1:{case}".encode()).hexdigest()}
    if case.startswith("lit:"):
        (request["question"],) = question
    elif case.startswith("qas:"):
        paper, request["question"] = question
        request["document"] = f"arxiv:{paper}"
    elif case.startswith("ei:"):
        paper, intervention, comparator, outcome = question
        request["document"] = f"PMC{paper}"
        request["question"] = (
            f"According to the specified report, how did {intervention} compare with "
            f"{comparator} with respect to {outcome}? Give the reported finding and its evidence."
        )
    else:
        raise ValueError(f"Unknown source: {case}")
    return audit, request


def litsearch(directory: Path):
    papers_by_row = pq.read_table(io.BytesIO(checked(directory, LIT))).to_pylist()
    parents = list(range(len(papers_by_row)))
    first: dict[int, int] = {}

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(papers_by_row):
        if not row["corpusids"]:
            raise ValueError(f"No LitSearch target on row {i}")
        for paper in row["corpusids"]:
            a, b = root(i), root(first.setdefault(paper, i))
            parents[max(a, b)] = min(a, b)
    for i, row in enumerate(papers_by_row):
        yield record(f"lit:{i:04}", f"lit:{root(i):04}", row["query_set"], row["query"])


def qasper(directory: Path):
    with tarfile.open(fileobj=io.BytesIO(checked(directory, QAS)), mode="r:gz") as tar:
        member = tar.extractfile("qasper-test-v0.3.json")
        if member is None:
            raise ValueError("Missing QASPER test data")
        data = json.load(member)
    buckets = {(False,): "answerable", (True,): "unanswerable", (False, True): "mixed"}
    for paper, doc in data.items():
        for q in doc["qas"]:
            votes = tuple(sorted({a["answer"]["unanswerable"] for a in q["answers"]}))
            yield record(
                f"qas:{q['question_id']}", f"arxiv:{paper}", buckets[votes], paper, q["question"]
            )


def csv_rows(data: bytes):
    return csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))


def evidence_inference(directory: Path):
    split = set(checked(directory, SPLIT).decode().split())
    labels: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ann in csv_rows(checked(directory, ANNOTATIONS)):
        if ann["PMCID"] in split and ann["Valid Label"] == "True":
            if ann["Label Code"] not in {"-1", "0", "1"}:
                raise ValueError("Unexpected validated direction")
            labels[ann["PMCID"], ann["PromptID"]].add(ann["Label Code"])
    for p in csv_rows(checked(directory, PROMPTS)):
        paper, prompt = p["PMCID"], p["PromptID"]
        if paper not in split:
            continue
        votes = labels[paper, prompt]
        state = (
            "no_valid_label" if not votes else "agreed_valid_label" if len(votes) == 1 else "mixed"
        )
        yield record(
            f"ei:{prompt}",
            f"PMC{paper}",
            state,
            paper,
            p["Intervention"],
            p["Comparator"],
            p["Outcome"],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--write-new", type=Path, help="create a new audit index, never overwrite")
    output.add_argument(
        "--emit-requests", type=Path, help="create a local question-only JSONL, never overwrite"
    )
    args = parser.parse_args()
    items = sorted(
        (*litsearch(args.sources), *qasper(args.sources), *evidence_inference(args.sources)),
        key=lambda item: item[0]["id"],
    )
    rows = [audit for audit, _ in items]
    ids = [r["id"] for r in rows]
    counts = Counter(i.split(":", 1)[0] for i in ids)
    if len(set(ids)) != len(rows) or counts != EXPECTED:
        raise ValueError(f"Invalid inventory: {counts}, {len(set(ids))} distinct IDs")
    payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode()
    if args.write_new:
        with args.write_new.open("xb") as out:
            out.write(payload)
    elif Path(__file__).with_name("public-index.jsonl").read_bytes() != payload:
        raise ValueError("Inventory no longer matches the published index")
    if args.emit_requests:
        requests = sorted((request for _, request in items), key=lambda r: r["key"])
        if len({r["key"] for r in requests}) != len(items):
            raise ValueError("Duplicate request key")
        with args.emit_requests.open("xb") as out:
            for request in requests:
                line = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
                out.write((line + "\n").encode())
    print(f"rows={len(rows)}, counts={dict(counts)}, sha256={hashlib.sha256(payload).hexdigest()}")


if __name__ == "__main__":
    main()
