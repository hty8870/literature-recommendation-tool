"""Run an explicitly labeled, deterministic Agent for browser contract and visual checks."""

from __future__ import annotations

import os
import shutil
import sys

import uvicorn
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult

from citation_tool.api import create_app
from citation_tool.config import PROJECT_ROOT, Settings

sys.path.insert(0, str(PROJECT_ROOT / "tests"))
from test_agent import ScriptedChatModel, StubReasonGenerator, tool_call  # noqa: E402


class ReplayChatModel(ScriptedChatModel):
    script: list[AIMessage]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        if self.received_messages and len(messages) < len(self.received_messages[-1]):
            self.responses = list(self.script)
        if not self.responses:
            self.responses.extend(self.script)
        return super()._generate(messages, stop, run_manager, **kwargs)


def main() -> None:
    test_root = PROJECT_ROOT / "data" / "runtime" / f"browser-fixture-{os.getpid()}"
    first = tool_call("browser-broad", ["LFY2", "soybean"], ["fixture-fast", "fixture-slow"])
    first.tool_calls[0]["args"]["options"]["library_delays_ms"] = {
        "fixture-fast": 250,
        "fixture-slow": 1600,
    }
    second = tool_call("browser-narrow", ["LFY2", "AP1"], ["fixture-fast"])
    second.tool_calls[0]["args"]["options"]["library_delays_ms"] = {"fixture-fast": 10}
    model = ReplayChatModel(
        responses=[],
        script=[
            first,
            second,
            AIMessage(content="第一次检索范围较宽，第二次已缩小到目标基因；两次结果分别保留。"),
            AIMessage(content="这是测试 Agent 对 Abstract 的自主答复；本轮不再调用检索。"),
        ]
    )
    try:
        app = create_app(
            Settings.for_tests(test_root),
            reason_generator=StubReasonGenerator(),
            agent_model=model,
        )
        uvicorn.run(app, host="127.0.0.1", port=8767, log_level="warning")
    finally:
        if test_root.exists():
            shutil.rmtree(test_root)


if __name__ == "__main__":
    main()
