"""Regression coverage for user-key routing and secure naming transport."""

import ssl
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from ebiz_deployment.config import UserMcpKeyQueryConfig
from ebiz_deployment.erp_session import SessionPrincipal
from ebiz_deployment.service_discovery import create_nacos_client
from ebiz_deployment.supply_chain_bff.app import BffContainer, create_app
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.dispatcher import BatchCoordinator
from ebiz_deployment.supply_chain_bff.level2_repository import ActiveReportExecution
from ebiz_deployment.supply_chain_bff.level2_worker import Level2Worker
from ebiz_deployment.user_mcp_credentials import UserMcpRefCodec


def settings(**changes):
    return BffSettings(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8013",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:legacy",
        user_bound_credentials_enabled=True,
        user_mcp_ref_codec=UserMcpRefCodec(b"s" * 32),
        legacy_batches_enabled=False,
        openclaw_tenant_id="tenant-a",
        **changes,
    )


def test_nacos_https_transport_verifies_certificate_and_disallows_redirect(monkeypatch):
    import ebiz_deployment.service_discovery as module

    handlers = []
    response = Mock()
    opener = Mock()
    opener.open.return_value = response

    def capture(*values):
        handlers.extend(values)
        return opener

    monkeypatch.setattr(module, "build_opener", capture, raising=False)
    client = create_nacos_client(
        "https://nacos.example.test:8848/",
        namespace="dev",
        username=None,
        password=None,
        timeout_seconds=2,
    )
    assert (
        client._do_sync_req(
            "/nacos/v1/ns/instance/list",
            params={"serviceName": "McpServer"},
            timeout=2,
            method="GET",
            module="naming",
        )
        is response
    )
    tls = next(handler for handler in handlers if hasattr(handler, "_context"))
    assert tls._context.verify_mode == ssl.CERT_REQUIRED
    assert tls._context.check_hostname
    redirects = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert redirects.redirect_request(None, None, 302, "redirect", {}, "https://other.test") is None
    assert opener.open.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize(
    "address", ["http://192.0.2.1:8848", "https://[::1]:8848", "https://nacos.test:8848\n"]
)
def test_nacos_configuration_rejects_unsafe_or_unsupported_origins(address):
    with pytest.raises(ValueError):
        UserMcpKeyQueryConfig(
            signing_secret_name="signing",
            timeout_seconds=2,
            server_addresses=address,
            allowed_hosts=("mcp.example.test",),
        )


@pytest.mark.asyncio
async def test_missing_legacy_reference_does_not_stop_reconciliation_or_fall_back():
    repository = AsyncMock()
    runtime = AsyncMock()
    runtime.get_execution.return_value = {"status": "RUNNING"}
    authorization = Mock(return_value="signed-runtime-token")
    worker = Level2Worker(
        repository=repository,
        runtime=runtime,
        artifacts=Mock(),
        settings=settings(),
        authorization_for_tenant=authorization,
        bus=None,
    )
    targets = [
        ActiveReportExecution(
            tenant_id="tenant-a",
            report_run_id=uuid4(),
            batch_id=uuid4(),
            execution_id=uuid4(),
            sequence=0,
            item_offset=0,
            item_count=1,
            credential_ref=reference,
        )
        for reference in (None, "signed-user-reference")
    ]
    assert await worker.reconcile_once(targets) == 1
    authorization.assert_called_once_with("tenant-a", credential_ref="signed-user-reference")
    runtime.get_execution.assert_awaited_once()
    assert repository.record_report_snapshot.await_args.kwargs["target"] == targets[1]


@pytest.mark.asyncio
async def test_compatibility_feedback_uses_authenticated_user_reference():
    configuration = settings(model_error_polish_enabled=True)
    runtime = AsyncMock()
    request_id = str(uuid4())
    runtime.model_feedback.return_value = {
        "title": "Unavailable",
        "summary": "Try again",
        "cause": "Data unavailable",
        "next_steps": ["Retry later"],
        "retryable": True,
        "request_id": request_id,
    }
    principal = SimpleNamespace(
        resolve=AsyncMock(return_value=SessionPrincipal("tenant-a", "alice", ("USER",)))
    )
    coordinator = BatchCoordinator(repository=Mock(), runtime=runtime, settings=configuration)
    authorization = Mock(return_value="signed-runtime-token")
    coordinator.authorization_for_tenant = authorization
    app = create_app(
        BffContainer(
            settings=configuration,
            repository=Mock(),
            runtime=runtime,
            coordinator=coordinator,
            cursor=CursorSigner(b"c" * 32, ttl=timedelta(days=1)),
            session_principal_client=principal,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as client:
        response = await client.post(
            "/api/supply-chain/v2/submission-feedback",
            headers={"Authorization": "Bearer synthetic-erp-token"},
            json={
                "request_id": request_id,
                "locale": "en-US",
                "operation": "START_ANALYSIS_BATCH",
                "facts": {
                    "error_code": "DATA_UNAVAILABLE",
                    "category": "transient",
                    "phase": "invocation",
                    "retryable": True,
                    "safe_message": "Data unavailable",
                },
            },
        )
    assert response.status_code == 200
    authorization.assert_called_once_with(
        "tenant-a",
        include_model_feedback=True,
        credential_ref=configuration.user_mcp_ref_codec.issue("tenant-a", "alice"),
    )
