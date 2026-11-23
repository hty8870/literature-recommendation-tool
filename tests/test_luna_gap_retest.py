from __future__ import annotations

import json
from copy import deepcopy

import pytest
from tools import luna_gap_retest as retest


def prepared_inputs(tmp_path, monkeypatch):
    suite = {
        "tasks": [
            {
                "case_id": "sf-1",
                "baseline_query": "Find evidence for the original claim.",
                "request": {"input_type": "abstract", "abstract": "Original claim."},
                "expect": {"evidence": {"gold-id-never-to-subject": []}},
            }
        ]
    }
    corpus = {"corpus_sha256": "fixed-corpus-hash"}
    source = tmp_path / "suite"
    source.mkdir()
    (source / "tasks.json").write_text("original task source", encoding="utf-8")
    monkeypatch.setattr(retest, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(retest, "SUITE_ROOT", source)
    monkeypatch.setattr(retest, "load_data", lambda: (suite, corpus))
    return suite, corpus


def test_retest_plan_is_explicit_development_and_does_not_leak_gold(tmp_path, monkeypatch):
    prepared_inputs(tmp_path, monkeypatch)
    root, _, _ = retest.prepare_run("test-run")
    original = (root / "plan.json").read_bytes()
    retest.prepare_run("test-run")
    assert (root / "plan.json").read_bytes() == original
    text = original.decode("utf-8")
    assert "not unseen holdout" in text
    assert "gpt-6-luna" in text
    assert "gold-id-never-to-subject" not in text
    assert "gold-id-never-to-subject" not in retest.baseline_prompt(
        retest.load_data()[0]["tasks"][0]
    )


def test_retest_changed_subject_prompt_cannot_reuse_frozen_name(tmp_path, monkeypatch):
    suite, corpus = prepared_inputs(tmp_path, monkeypatch)
    retest.prepare_run("test-run")
    changed = deepcopy(suite)
    changed["tasks"][0]["baseline_query"] = "A different user request."
    monkeypatch.setattr(retest, "load_data", lambda: (changed, corpus))
    with pytest.raises(ValueError, match="frozen retest plan changed"):
        retest.prepare_run("test-run")


def saved_baseline(tmp_path, monkeypatch):
    suite, _ = prepared_inputs(tmp_path, monkeypatch)
    root, _, _ = retest.prepare_run("source")
    corpus = retest.RUNTIME / "corpus.json"
    corpus.write_text('{"records":[]}', encoding="utf-8")
    prompt = root / "prompts" / "sf-1.txt"
    prompt.parent.mkdir()
    prompt.write_text(retest.baseline_prompt(suite["tasks"][0]), encoding="utf-8")
    record = {
        "case_id": "sf-1",
        "system": "kimi_code",
        "completed": True,
        "model_valid": True,
        "model_observed": ["gpt-6-luna"],
        "session_id": "isolated-baseline",
        "prompt_sha256": retest.file_hash(prompt),
        "corpus_sha256": retest.file_hash(corpus),
        "response_text": '{"papers":[],"conclusion":"No evidence was obtained."}',
    }
    path = root / "kimi" / "sf-1.json"
    path.parent.mkdir()
    path.write_text(json.dumps(record), encoding="utf-8")
    return root, path, record


def test_reuse_keeps_frozen_answer_and_identity_without_running_models(tmp_path, monkeypatch):
    source, path, original = saved_baseline(tmp_path, monkeypatch)
    before = path.read_bytes()
    result = retest.reuse_baseline("destination", "source")
    assert result["baseline_reused"] == 1
    assert result["new_model_calls"] == 0
    copied = json.loads((source.parent / "destination/kimi/sf-1.json").read_text())
    assert copied["response_text"] == original["response_text"]
    assert copied["session_id"] == original["session_id"]
    assert copied["baseline_reused_from"]["record_sha256"] == retest.file_hash(path)
    assert path.read_bytes() == before
    assert retest.reuse_baseline("destination", "source") == result


@pytest.mark.parametrize("invalid", ["model", "prompt", "corpus"])
def test_reuse_rejects_unverified_or_changed_subjects(tmp_path, monkeypatch, invalid):
    source, path, original = saved_baseline(tmp_path, monkeypatch)
    if invalid == "model":
        original["model_observed"] = ["different-model"]
        path.write_text(json.dumps(original), encoding="utf-8")
    elif invalid == "prompt":
        (source / "prompts/sf-1.txt").write_text("different request", encoding="utf-8")
    else:
        (retest.RUNTIME / "corpus.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        retest.reuse_baseline("destination", "source")
    assert not (source.parent / "destination/kimi/sf-1.json").exists()


@pytest.mark.parametrize("name", ["../escape", "folder/nested", "", "a" * 91])
def test_retest_name_cannot_escape_output_directory(name):
    with pytest.raises(ValueError, match="invalid run name"):
        retest.prepare_run(name)
