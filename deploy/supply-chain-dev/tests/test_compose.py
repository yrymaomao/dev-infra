"""Parse the published transport overlay without Docker or dev credentials."""

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "transport.compose.yaml"


def load_compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["nacos-transport", "mcp-transport", "mq-transport"])
def test_transport_tmpfs_is_one_complete_mount(name):
    service = load_compose()["services"][name]
    assert service["tmpfs"] == ["/tmp:size=16m,noexec,nosuid,nodev"]
    assert service["network_mode"] == "service:app-network"


@pytest.mark.parametrize(
    "name", ["app-network", "nacos-transport", "mcp-transport", "mq-transport"]
)
def test_services_keep_the_isolated_unprivileged_boundary(name):
    service = load_compose()["services"][name]
    assert not service.get("ports")
    assert not service.get("privileged", False)
    assert service.get("network_mode") != "host"
    assert service["user"] == "65532:65532"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_transport_reuses_only_the_existing_dev_network():
    compose = load_compose()
    assert compose["networks"] == {"existing-dev": {"external": True, "name": "supply-chain-dev"}}
    assert compose["services"]["app-network"]["networks"] == ["existing-dev"]
