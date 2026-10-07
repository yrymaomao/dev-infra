"""Regression for the published Adapter operation cancellation fields."""

import pytest

from ebiz_deployment.openclaw_reception.conversation_contracts import OperationView
from ebiz_deployment.openclaw_reception.conversation_repository import (
    project_event,
    project_operation_update,
)


@pytest.mark.parametrize("requested", [False, True])
def test_cancel_flag_is_public_boolean_not_an_unknown_adapter_field(requested):
    payload = {
        "operation_id": "operation-1",
        "execution_id": "execution-1",
        "state": "running",
        "cancel_requested": requested,
    }
    assert project_event("operation.updated", payload) == payload
    assert OperationView(**payload).cancel_requested is requested


def test_cancel_error_and_identity_survive_subsequent_partial_operation_event():
    error = {"code": "GATEWAY_UNAVAILABLE", "safe_message": "Try status later", "retryable": True}
    current = project_event(
        "operation.updated",
        {
            "operation_id": "operation-1",
            "execution_id": "execution-1",
            "state": "running",
            "cancel_requested": True,
            "cancel_error": error,
        },
    )
    result = project_operation_update(current, {"operation_id": "operation-1", "state": "running"})
    view = OperationView(**result)
    assert view.execution_id == "execution-1"
    assert view.cancel_requested is True
    assert view.cancel_error is not None and view.cancel_error.code == error["code"]


@pytest.mark.parametrize(
    "extra",
    [
        {"cancel_requested": "false"},
        {"cancel_requested": 0},
        {
            "cancel_requested": False,
            "cancel_error": {"code": "X", "safe_message": "x", "retryable": True},
        },
        {
            "cancel_requested": True,
            "cancel_error": {"code": "X", "safe_message": "x", "retryable": "true"},
        },
        {
            "cancel_requested": True,
            "cancel_error": {"code": "X", "safe_message": "x", "retryable": True, "raw": "hidden"},
        },
        {
            "cancel_requested": True,
            "cancel_error": {"code": "X", "safe_message": "x" * 1025, "retryable": True},
        },
    ],
)
def test_cancel_projection_rejects_invalid_types_and_private_fields(extra):
    with pytest.raises(ValueError):
        project_event(
            "operation.updated", {"operation_id": "operation-1", "state": "running", **extra}
        )
