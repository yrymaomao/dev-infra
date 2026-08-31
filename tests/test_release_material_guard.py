from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ebiz_deployment.release_material_guard import ReleaseMaterialError, verify_release_material


def _wheel(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_guard_checks_only_explicit_diff_and_wheel_material(tmp_path: Path) -> None:
    selected = tmp_path / "deployment.json"
    selected.write_text(
        '{"endpoint":"${MCP_ENDPOINT}","local":"http://127.0.0.1:18081/mcp"}',
        encoding="utf-8",
    )
    unselected = tmp_path / "operator-local.env"
    unselected.write_text("OPENAI_API_KEY=" + "sk-" + "x" * 40, encoding="utf-8")
    wheel = _wheel(
        tmp_path / "release.whl",
        {
            "package/config.json": '{"endpoint":"${OPENAI_ENDPOINT}"}',
            "package/schema.yaml": "$schema: https://json-schema.org/draft/2020-12/schema",
        },
    )

    verified = verify_release_material((selected, wheel))

    assert verified == (selected.resolve(), wheel.resolve())


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "a" * 40,
        "mcp_" + "b" * 32,
        "eyJ" + "a" * 20 + "." + "eyJ" + "b" * 20 + "." + "c" * 24,
    ],
)
def test_guard_rejects_credential_shapes_in_selected_diff_file(tmp_path: Path, secret: str) -> None:
    changed = tmp_path / "deployment.json"
    changed.write_text('{"credential":"' + secret + '"}', encoding="utf-8")

    with pytest.raises(ReleaseMaterialError, match="credential-shaped value"):
        verify_release_material((changed,))


def test_guard_rejects_credential_shapes_inside_wheel(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "release.whl",
        {"package/environment.json": "X_MCP_KEY=" + "mcp_" + "c" * 32},
    )

    with pytest.raises(ReleaseMaterialError, match="package/environment.json"):
        verify_release_material((wheel,))


def test_guard_rejects_credential_shape_inside_binary_framed_wheel_member(tmp_path: Path) -> None:
    wheel = tmp_path / "release.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/data.bin", b"\xff\xfe" + b"sk-" + b"d" * 40)

    with pytest.raises(ReleaseMaterialError, match="package/data.bin"):
        verify_release_material((wheel,))


def test_guard_rejects_concrete_non_loopback_runtime_endpoint(tmp_path: Path) -> None:
    changed = tmp_path / "deployment.json"
    changed.write_text(
        '{"mcp_endpoint":"' + "https://" + 'erp.vendor.internal/mcp"}',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseMaterialError, match="fixed non-loopback runtime endpoint"):
        verify_release_material((changed,))


def test_guard_rejects_default_production_base_url_constant(tmp_path: Path) -> None:
    changed = tmp_path / "config.py"
    changed.write_text(
        'DEFAULT_BASE_URL = "' + "https://" + 'model.vendor.internal/v1"',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseMaterialError, match="fixed non-loopback runtime endpoint"):
        verify_release_material((changed,))


def test_guard_allows_reserved_documentation_endpoint(tmp_path: Path) -> None:
    changed = tmp_path / "README.md"
    changed.write_text('endpoint: "https://erp-gateway.example.com/mcp"', encoding="utf-8")

    assert verify_release_material((changed,)) == (changed.resolve(),)


def test_guard_does_not_treat_schema_uri_constants_as_runtime_endpoints(tmp_path: Path) -> None:
    changed = tmp_path / "schema_registry.py"
    changed.write_text(
        'IDENTITY_URI = "https://schemas.ebizhub.com/vocab/runtime-identity/v1"',
        encoding="utf-8",
    )

    assert verify_release_material((changed,)) == (changed.resolve(),)


def test_guard_fails_closed_for_missing_or_directory_material(tmp_path: Path) -> None:
    with pytest.raises(ReleaseMaterialError, match="regular file"):
        verify_release_material((tmp_path / "missing.json",))
    with pytest.raises(ReleaseMaterialError, match="regular file"):
        verify_release_material((tmp_path,))


def test_guard_fails_closed_when_no_release_material_is_selected() -> None:
    with pytest.raises(ReleaseMaterialError, match="at least one"):
        verify_release_material(())
