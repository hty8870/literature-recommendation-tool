import pytest
from tools.gap_benchmark import opencode_receipt_budget as trial


def test_budget_pairs_are_balanced_across_families_and_repetitions():
    order = trial.dispatch_order()
    assert len(order) == len(set(order)) == 16
    for case in trial.CASES:
        first = [cap for c, rep, cap in order if c == case and rep == 1]
        later = [cap for c, rep, cap in order if c == case and rep == 2]
        assert set(first) == set(later) == {8192, 16384}
        assert first == later[::-1]


def test_report_rejects_missing_arms_and_reused_provider_responses():
    with pytest.raises(ValueError, match="every registered budget"):
        trial.summarize({})
    rows = {
        key: {"call_metadata": [{"response_id": "not isolated"}]} for key in trial.dispatch_order()
    }
    with pytest.raises(ValueError, match="reused"):
        trial.summarize(rows)
