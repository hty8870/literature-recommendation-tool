from __future__ import annotations

import pytest

from citation_tool.config import LibraryConfig
from citation_tool.entities import ClaimContext
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.reasons import (
    DESIGN_ATTRIBUTE_PATTERNS,
    _has_explicit_design,
    reason_entities_grounded,
    unlinked_design_violations,
    unsupported_design_attributes,
)
from citation_tool.relevance import query_plan, record_allowed, semantic_query_text
from test_units import make_candidate, make_claim


def abstract_claim(text: str) -> ClaimContext:
    return make_claim(text=text).model_copy(
        update={"input_type": "abstract", "query_terms": [], "topic_context": []}
    )


def keyword_claim(text: str) -> ClaimContext:
    return make_claim(text=text).model_copy(
        update={"input_type": "keywords", "query_terms": text.split(), "topic_context": []}
    )


def record(doi: str, title: str, abstract: str) -> dict:
    return {
        "work_id": f"work:{doi}",
        "paper_id": f"paper:{doi}",
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "year": 2025,
        "authors": "Example",
        "pub_types": ["research-article"],
        "source_metadata_url": "https://example.org/article",
        "simulation": False,
    }


@pytest.mark.parametrize(
    "text",
    [
        "Gene expression does not vary appreciably across genetically identical cells.",
        "Suboptimal nutrition is not predictive of chronic disease",
        "CHEK2 is not associated with breast cancer.",
        "Thigh-length graduated compression stockings did not reduce deep vein thrombosis.",
        "Mice without IFN-γ or its receptor are resistant to EAM.",
        "The trial did not include patients with renal impairment.",
        "该基因的表达不随年龄变化。",
        "该蛋白并未参与细胞凋亡。",
        "土壤干旱没有降低该品种的光合速率。",
    ],
)
def test_scientific_negation_keeps_the_full_semantic_query(text: str) -> None:
    claim = abstract_claim(text)
    assert semantic_query_text(claim) == text


def test_imperative_exclusion_instruction_is_still_an_instruction() -> None:
    claim = abstract_claim("Do not include case reports. Find RCTs of the drug.")
    assert semantic_query_text(claim) == "Find RCTs of the drug."


@pytest.mark.parametrize(
    "text",
    [
        "Localization of PIN1 in the roots of Arabidopsis does not require VPS9a",
        "Suboptimal nutrition is not predictive of chronic disease",
    ],
)
def test_scientific_negation_keeps_terms_and_never_becomes_an_exclusion(text: str) -> None:
    plan = query_plan(abstract_claim(text))
    assert plan.excluded_identifiers == ()
    assert plan.excluded_topics == ()
    assert "require" in plan.terms or "predictive" in plan.terms
    assert "chronic" in plan.terms or "vps9a" in plan.identifiers


def test_negated_claim_still_retrieves_the_study_it_disputes() -> None:
    records = [
        record(
            "10.1007/vps9a",
            "VPS9a-dependent PIN1 localization in Arabidopsis roots",
            "PIN1 localization requires the Arabidopsis guanine nucleotide exchange factor VPS9a.",
        ),
        record("10.1007/unrelated", "Cell biology", "A different topic entirely."),
    ]
    adapter = MetadataCorpusAdapter(
        LibraryConfig("methods", "Methods", "fast", 0, simulation=False), records
    )
    claim = abstract_claim("Localization of PIN1 in Arabidopsis roots does not require VPS9a")
    assert [item.passage.work_id for item in adapter.search(claim)] == ["work:10.1007/vps9a"]
    assert record_allowed(
        claim,
        records[0]["title"],
        records[0]["abstract"],
        records[0]["doi"],
    )


def test_explicit_exclusion_instructions_still_filter_candidates() -> None:
    records = [
        record(
            "10.1007/soybean",
            "Floral organ identity in soybean",
            "LFY2 controls floral organ identity in soybean.",
        ),
        record(
            "10.1007/arabidopsis",
            "Floral organ identity in Arabidopsis",
            "AP1 controls floral organ identity in Arabidopsis.",
        ),
    ]
    adapter = MetadataCorpusAdapter(
        LibraryConfig("methods", "Methods", "fast", 0, simulation=False), records
    )
    chinese = keyword_claim("不要大豆，只找拟南芥开花")
    assert [item.passage.work_id for item in adapter.search(chinese)] == [
        "work:10.1007/arabidopsis"
    ]
    assert query_plan(chinese).excluded_topics == ("soybean",)
    query = semantic_query_text(chinese)
    assert "拟南芥" in query and "开花" in query
    assert "大豆" not in query

    without = keyword_claim("LFY1 without LFY2")
    assert query_plan(without).excluded_identifiers == ("lfy2",)
    abstract_without = abstract_claim("Find LFY1 studies without measuring LFY2")
    plan = query_plan(abstract_without)
    assert plan.excluded_identifiers == ("lfy2",)
    assert "without measuring" not in semantic_query_text(abstract_without)


def test_exclusion_instruction_keeps_the_positive_clause_that_follows_it() -> None:
    claim = keyword_claim("排除仅分析开花期关联的论文，在大豆中寻找原始功能研究")
    query = semantic_query_text(claim)
    assert "大豆" in query and "原始功能研究" in query
    assert "开花期" not in query


def test_scientific_negation_survives_alongside_an_exclusion_instruction() -> None:
    text = (
        "Exclude reviews; plants lacking the receptor do not accumulate the metabolite. "
        "We need primary experiments."
    )
    query = semantic_query_text(abstract_claim(text))
    assert "Exclude reviews" not in query
    assert "do not accumulate the metabolite" in query
    assert "primary experiments" in query


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("该研究采用随机模拟估计参数。", False),
        ("该模型使用随机表达描述噪声。", False),
        ("随机过程驱动表达波动。", False),
        ("The randomized simulation used stochastic rounding.", False),
        ("A randomized algorithm was benchmarked.", False),
        ("该研究为随机对照试验。", True),
        ("该研究为随机、双盲、安慰剂对照试验。", True),
        ("采用随机、对照设计。", True),
        ("随机、确定性模型都用于预测。", False),
        ("患者被随机分配至两组。", True),
        ("研究采用随机化设计。", True),
        ("A randomized, double-blind, placebo-controlled trial.", True),
        ("Participants were randomly assigned to placebo.", True),
    ],
)
def test_randomized_design_requires_assignment_not_a_random_process(
    text: str, expected: bool
) -> None:
    assert _has_explicit_design(text, DESIGN_ATTRIBUTE_PATTERNS["randomized"]) is expected


def test_stochastic_simulation_reason_is_grounded_but_trial_claim_is_not() -> None:
    simulation = make_candidate(
        raw_text="We used a stochastic simulation of gene expression noise.",
        quote="We used a stochastic simulation of gene expression noise.",
    )
    assert reason_entities_grounded("该研究用随机模拟表达噪声，属建模工作。", simulation)
    assert unsupported_design_attributes("该研究用随机模拟表达噪声。", [simulation.context]) == []
    assert not reason_entities_grounded("该研究采用随机对照试验。", simulation)
    assert unsupported_design_attributes("该研究采用随机对照试验。", [simulation.context]) == [
        "randomized"
    ]
    sources = [
        {
            "title": "Stochastic simulation of expression noise",
            "source_text": "We used a stochastic simulation of gene expression noise.",
        }
    ]
    assert unlinked_design_violations("该研究使用随机模拟表达噪声。", sources) == []


def test_trial_randomization_in_source_still_permits_the_attribute() -> None:
    trial = make_candidate(
        raw_text="A randomized, placebo-controlled trial of the drug.",
        quote="A randomized, placebo-controlled trial of the drug.",
    )
    assert reason_entities_grounded("该研究采用随机分组设计。", trial)
