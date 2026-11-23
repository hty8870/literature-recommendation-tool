import asyncio
import uuid

import pytest
from pydantic import PrivateAttr
from tools.luna_native_product_retest import NativeModelAudit, NativeModelSlots, collect_model_audit
from tools.parallel_luna import CancelTrackedChatOpenAI
from tools.scifact_workspace_retest import settle_case


async def test_drain_waits_for_audited_callback_before_sealing(tmp_path):
    audit = NativeModelAudit(tmp_path, NativeModelSlots())
    call_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=call_id)
    task = asyncio.create_task(settle_case(audit, drain_seconds=1))
    await asyncio.sleep(0)
    assert not task.done()
    await audit.on_llm_error(RuntimeError(), run_id=call_id)
    error, _ = await task
    assert error is None
    assert audit.slots.active == 0


async def test_drain_preserves_the_unverified_state_when_callback_does_not_finish(tmp_path):
    audit = NativeModelAudit(tmp_path, NativeModelSlots())
    call_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=call_id)
    error, _ = await settle_case(audit, drain_seconds=0)
    assert error == "NATIVE_MODEL_DRAIN_TIMEOUT"
    assert audit.slots.active == 1
    await audit.on_llm_error(RuntimeError(), run_id=call_id)
    assert audit.slots.active == 0


class WaitingModel(CancelTrackedChatOpenAI):
    _entered = PrivateAttr(default_factory=asyncio.Event)

    async def _agenerate(self, *args, **kwargs):
        self._entered.set()
        await asyncio.Event().wait()


async def test_cancelled_ainvoke_closes_only_its_own_audit_without_validating_it(tmp_path):
    audit = NativeModelAudit(tmp_path, NativeModelSlots())
    other = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=other)
    model = WaitingModel(model="gpt-6-luna", api_key="fixture", callbacks=[audit])  # noqa: S106
    model._close_cancelled = audit.close_cancelled_invocation
    task = asyncio.create_task(model.ainvoke("public fixture"))
    await asyncio.wait_for(model._entered.wait(), timeout=2)
    assert audit.slots.active == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert audit.slots.active == 1
    artifacts = collect_model_audit(tmp_path)
    cancelled = next(row for row in artifacts["artifacts"] if row["call_id"] != str(other))
    assert cancelled["status"] == "failed"
    assert cancelled["error"] == {"type": "CancelledError"}
    assert cancelled["response_ids"] == []
    assert artifacts["model_valid"] is False
    await audit.on_llm_error(RuntimeError(), run_id=other)
    assert (await settle_case(audit, drain_seconds=1))[0] is None
