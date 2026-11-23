import pytest
from tools import scifact_workspace_resume as resume


async def test_loaded_index_backpressure_reports_both_limits(monkeypatch):
    memory = {"free_commit_bytes": resume.GIB, "free_physical_bytes": 8 * resume.GIB}
    monkeypatch.setattr(resume, "memory_status", lambda: memory)
    with pytest.raises(resume.MemoryBackpressure, match="frozen checkpoints preserved") as err:
        await resume.wait_for_memory(2 * resume.GIB, "next isolated case", wait_s=0)
    assert err.value.reading == memory


async def test_enough_headroom_is_admitted_without_recycling(monkeypatch):
    memory = {"free_commit_bytes": 3 * resume.GIB, "free_physical_bytes": 8 * resume.GIB}
    monkeypatch.setattr(resume, "memory_status", lambda: memory)
    assert await resume.wait_for_memory(2 * resume.GIB, "next isolated case", wait_s=0) == memory
