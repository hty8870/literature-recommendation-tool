from tools.luna_comparison_report import METRICS, render


def test_report_keeps_negative_verdict_actual_model_and_separate_metrics():
    statistics = {key: 0 for _, key in METRICS}
    statistics.update(runs=1, completed=1)
    assessment = {
        "systems": {"product": statistics, "kimi_code": statistics},
        "paired": {
            "paired": {
                "preregistered_practical_advantage_observed": False,
                "positive_families": 1,
                "product_win_families": 0,
                "tie_families": 1,
                "kimi_win_families": 0,
                "family_mean_difference": 0,
                "descriptive_cluster_bootstrap_95_interval": [-0.1, 0.1],
            },
            "slices": {},
        },
        "records": [
            {"system": system, "case_id": "case-1", "assessment": {}}
            for system in ("product", "kimi_code")
        ],
    }
    contract = {
        "expected_model": "fixture-native-model",
        "transport": "native-fixture",
        "implementation_hash": "frozen-fixture",
        "corpus_records": 1,
        "tasks": 1,
    }
    engineering = {
        "model_valid": 1,
        "cases": 1,
        "reason_generated": 1,
        "reason_total": 1,
        "ranking_completed": 1,
        "ranking_total": 1,
        "max_observed_model_inflight": 1,
        "logical_model_calls": 1,
        "usage_measurement": "observable lower bound",
    }
    text = render("fixture", assessment, contract, engineering)
    assert "未达到" in text
    assert "fixture-native-model" in text and "deepseek-flash" not in text
    assert "完整 rationale" in text and "方向正确" in text
    assert "不是精确物理" in text and "不能" in text
