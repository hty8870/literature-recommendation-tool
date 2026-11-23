from __future__ import annotations

import pytest

from citation_tool.online import native_query
from citation_tool.retrieval import SourceSearchError
from test_units import make_claim


def test_full_text_passage_preserves_query_and_scientific_negation():
    text = "LFY2 does not change soybean flowering time under the reported conditions."
    claim = make_claim(text=text).model_copy(
        update={"input_type": "full_text", "topic_context": [], "query_terms": []}
    )
    query = native_query(claim)
    assert query == f"({text}) AND HAS_ABSTRACT:y"
    assert "TITLE_ABS:" not in query


def test_full_text_passage_with_explicit_topics_uses_topics_without_sending_the_document():
    claim = make_claim(text="Private paragraph not needed by the remote database.").model_copy(
        update={"input_type": "full_text", "topic_context": ["LFY2", "soybean"]}
    )
    query = native_query(claim)
    assert 'TITLE_ABS:"LFY2"' in query
    assert 'TITLE_ABS:"soybean"' in query
    assert "Private" not in query


def test_oversized_full_text_passage_is_not_silently_truncated():
    claim = make_claim(text="X" * 4_001).model_copy(
        update={"input_type": "full_text", "topic_context": []}
    )
    with pytest.raises(SourceSearchError) as caught:
        native_query(claim)
    assert caught.value.code == "QUERY_TOO_LONG"
