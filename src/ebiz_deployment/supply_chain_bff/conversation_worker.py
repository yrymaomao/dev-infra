"""A durable consumer of Adapter events, independent of browser connections."""

import asyncio
import logging
from uuid import uuid4

import httpx

from .config import BffSettings
from .conversation_repository import ConversationRepository

logger = logging.getLogger(__name__)


class ConversationWorker:
    def __init__(
        self,
        repository: ConversationRepository,
        settings: BffSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.repository, self.settings, self.transport = repository, settings, transport
        self.owner = uuid4().hex

    async def step(self, client: httpx.AsyncClient) -> bool:
        row = await self.repository.claim(self.owner)
        if not row:
            return False
        endpoint = self.settings.openclaw_ingress_url.rsplit("/", 1)[0] + "/turns"
        response = await client.get(
            endpoint, params={"occurrence": str(row["id"]), "after": row["sequence"]}
        )
        if response.status_code == 404 and row["sequence"] == 0:
            response = await client.post(
                endpoint,
                json={
                    "selector": f"turn:{row['id']}",
                    "occurrence": str(row["id"]),
                    "prompt": row["prompt"],
                },
            )
        elif response.status_code == 404:
            event = {
                "sequence": row["sequence"] + 1,
                "kind": "turn.failed",
                "payload": {
                    "code": "TURN_INTERRUPTED",
                    "safe_message": (
                        "The assistant history is unavailable. Previously submitted analysis "
                        "was not resubmitted; its recorded operation must be checked."
                    ),
                },
            }
            await self.repository.ingest(
                row["id"], self.owner, row["tenant"], [event], "interrupted", event["sequence"]
            )
            return True
        if response.status_code in (401, 403):
            event = {
                "sequence": row["sequence"] + 1,
                "kind": "turn.failed",
                "payload": {
                    "code": "ASSISTANT_ACCESS_DENIED",
                    "safe_message": (
                        "The assistant connection was not authorized. No analysis was resubmitted."
                    ),
                },
            }
            await self.repository.ingest(
                row["id"], self.owner, row["tenant"], [event], "failed", event["sequence"]
            )
            return True
        response.raise_for_status()
        try:
            value = response.json()
            if value.get("state") not in {
                "running",
                "completed",
                "failed",
                "interrupted",
            } or not isinstance(value.get("events"), list):
                raise ValueError("Invalid adapter snapshot")
            await self.repository.ingest(
                row["id"],
                self.owner,
                row["tenant"],
                value["events"],
                value["state"],
                value["sequence"],
            )
        except (ValueError, KeyError, TypeError):
            event = {
                "sequence": row["sequence"] + 1,
                "kind": "turn.failed",
                "payload": {
                    "code": "CONVERSATION_RESPONSE_INVALID",
                    "safe_message": (
                        "The assistant result could not be validated. No analysis was resubmitted."
                    ),
                },
            }
            await self.repository.ingest(
                row["id"], self.owner, row["tenant"], [event], "failed", event["sequence"]
            )
        return True

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            timeout=5,
            headers={"Authorization": f"Bearer {self.settings.openclaw_ingress_credential}"},
        ) as client:
            while not stop.is_set():
                try:
                    await self.step(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # No response bodies, addresses, prompt or exception strings in logs.
                    logger.warning("conversation_worker_poll_unavailable")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.1)
                except TimeoutError:
                    pass
