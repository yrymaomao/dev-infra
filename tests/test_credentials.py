from __future__ import annotations

import pytest

from ebiz_deployment.credentials import EnvironmentSecretResolver


@pytest.mark.asyncio
async def test_environment_secret_resolver_enforces_symbolic_allowlist_and_redacts() -> None:
    resolver = EnvironmentSecretResolver(
        {"model_key": "MODEL_KEY_ENV"}, {"MODEL_KEY_ENV": "top-secret-model-value"}
    )
    assert await resolver.resolve("model_key") == "top-secret-model-value"
    with pytest.raises(ValueError) as error:
        await resolver.resolve("other_key")
    assert "top-secret-model-value" not in repr(resolver)
    assert "top-secret-model-value" not in str(error.value)
