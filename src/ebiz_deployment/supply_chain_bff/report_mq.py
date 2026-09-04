"""RabbitMQ transport for opaque report-batch events with publisher confirms and DLX."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ReportBatchMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    schema_version: int = Field(ge=1, le=1)
    report_run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    batch_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    selection_snapshot_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    batch_no: int = Field(ge=1, le=50)
    item_count: int = Field(ge=1, le=200)
    policy_snapshot_ref: str = Field(min_length=1, max_length=512)
    trace_id: str = Field(min_length=1, max_length=128)
    occurred_at: str = Field(min_length=20, max_length=64)


class DeferredDelivery(RuntimeError):
    """A bounded, recoverable condition that should be redelivered."""


MessageHandler = Callable[[ReportBatchMessage], Awaitable[None]]


class ReportMessageBus(Protocol):
    async def connect(self) -> None: ...

    async def publish(self, payload: Mapping[str, object]) -> None: ...

    async def consume(self, handler: MessageHandler, *, stop: asyncio.Event) -> None: ...

    async def close(self) -> None: ...


class AioPikaReportBus:
    """One robust connection owns both publisher and consumer for this BFF."""

    def __init__(
        self,
        *,
        url: str,
        exchange_name: str,
        queue_name: str,
        routing_key: str,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._queue_name = queue_name
        self._routing_key = routing_key
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._queue: Any = None

    async def connect(self) -> None:
        aio_pika = importlib.import_module("aio_pika")
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        await self._channel.set_qos(prefetch_count=8)
        exchange_type = aio_pika.ExchangeType.DIRECT
        dlx = await self._channel.declare_exchange(
            f"{self._exchange_name}.dlx",
            exchange_type,
            durable=True,
        )
        dlq = await self._channel.declare_queue(
            f"{self._queue_name}.dlq",
            durable=True,
        )
        await dlq.bind(dlx, routing_key=self._routing_key)
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name,
            exchange_type,
            durable=True,
        )
        self._queue = await self._channel.declare_queue(
            self._queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": f"{self._exchange_name}.dlx",
                "x-dead-letter-routing-key": self._routing_key,
            },
        )
        await self._queue.bind(self._exchange, routing_key=self._routing_key)

    async def publish(self, payload: Mapping[str, object]) -> None:
        if self._exchange is None:
            raise RuntimeError("RabbitMQ bus is not connected")
        aio_pika = importlib.import_module("aio_pika")
        validated = ReportBatchMessage.model_validate(dict(payload))
        body = json.dumps(
            validated.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=validated.event_id,
            correlation_id=validated.report_run_id,
            headers={"schema_version": 1},
        )
        confirmed = await self._exchange.publish(
            message,
            routing_key=self._routing_key,
            mandatory=True,
        )
        if confirmed is False:
            raise RuntimeError("RabbitMQ publisher confirm was negative")

    async def consume(self, handler: MessageHandler, *, stop: asyncio.Event) -> None:
        if self._queue is None:
            raise RuntimeError("RabbitMQ bus is not connected")
        async with self._queue.iterator() as iterator:
            async for message in iterator:
                if stop.is_set():
                    break
                try:
                    raw = json.loads(message.body)
                    validated = ReportBatchMessage.model_validate(raw)
                    await handler(validated)
                except DeferredDelivery:
                    await message.nack(requeue=True)
                    await asyncio.sleep(0.25)
                except Exception:
                    await message.reject(requeue=False)
                else:
                    await message.ack()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = None
        self._channel = None
        self._exchange = None
        self._queue = None
