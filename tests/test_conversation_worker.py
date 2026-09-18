from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from ebiz_deployment.openclaw_reception.conversation_worker import ConversationWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_adapter_denial_terminates_turn_without_retry_or_business_claim(status):
    repository = SimpleNamespace(
        claim=AsyncMock(return_value={"id": uuid4(), "sequence": 2, "tenant": "a"}),
        ingest=AsyncMock(),
    )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status)

    worker = ConversationWorker(
        repository, ingress_url="http://127.0.0.1/ingress", ingress_credential="s" * 32
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await worker.step(client)
    assert len(requests) == 1 and requests[0].method == "GET"
    event = repository.ingest.call_args.args[3][0]
    assert event["payload"]["code"] == "ASSISTANT_ACCESS_DENIED"
    assert repository.ingest.call_args.args[4] == "failed"
