import asyncio

from langchain_core.tools import StructuredTool
from tools.scifact_dsh_retest import ACTIVE_GRAPHS, DshGraph, routed_factory


async def test_factory_isolated_between_parallel_subjects():
    async def search(query: str) -> dict:
        return {"query": query}

    tool = StructuredTool.from_function(
        coroutine=search, name="search_literature", description="Isolated test search."
    )
    control_calls = []

    def original(**kwargs):
        control_calls.append(kwargs["system_prompt"])
        return "control"

    adapter = routed_factory(original)

    async def subject(name, graphs):
        token = ACTIVE_GRAPHS.set(graphs)
        try:
            await asyncio.sleep(0)
            return adapter(model=None, tools=[tool], system_prompt=name, middleware=[])
        finally:
            ACTIVE_GRAPHS.reset(token)

    graphs = []
    original_agent, dsh_agent = await asyncio.gather(
        subject("control", None), subject("dsh", graphs)
    )
    assert original_agent == "control"
    assert isinstance(dsh_agent, DshGraph) and graphs == [dsh_agent]
    assert control_calls == ["control"]
    assert ACTIVE_GRAPHS.get() is None
