"""Runtime configuration startup boundary tests."""

from __future__ import annotations

import pytest
import yaml

from src.api import server


def test_runtime_config_missing_file_aborts_startup(monkeypatch, tmp_path):
    missing_path = tmp_path / "missing-config.yaml"
    monkeypatch.setattr(server, "_RUNTIME_CONFIG_PATH", missing_path)

    with pytest.raises(RuntimeError, match="Unable to load required runtime config") as exc_info:
        server._load_runtime_config()

    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_runtime_config_malformed_yaml_aborts_startup(monkeypatch, tmp_path):
    malformed_path = tmp_path / "config.yaml"
    malformed_path.write_text("scoring: [", encoding="utf-8")
    monkeypatch.setattr(server, "_RUNTIME_CONFIG_PATH", malformed_path)

    with pytest.raises(RuntimeError, match="Unable to load required runtime config") as exc_info:
        server._load_runtime_config()

    assert isinstance(exc_info.value.__cause__, yaml.YAMLError)


@pytest.mark.parametrize("config_text", ["", "- not\n- a mapping\n"])
def test_runtime_config_non_mapping_aborts_startup(monkeypatch, tmp_path, config_text):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(server, "_RUNTIME_CONFIG_PATH", config_path)

    with pytest.raises(RuntimeError, match="must contain a YAML mapping"):
        server._load_runtime_config()
