import json

import pytest

from trinity.coordinator import Refused

from trinity.config import resolve_config, render_summary


@pytest.mark.parametrize("preset,models,interval", [
    ("economy", ["luna", "sol", "astra"], 900),
    ("balanced", ["luna", "opus", "astra"], 300),
    ("intensive", ["sol", "opus", "astra"], 180),
    ("research", ["luna", "opus", "astra"], 600),
])
def test_presets_resolve_role_models_providers_and_interval(preset, models, interval):
    assert resolve_config is not None, "configuration resolver is missing"
    config = resolve_config(preset)
    assert [config["agents"][r]["model"] for r in ("worker", "supervisor", "consultant")] == models
    assert config["checkpoints"]["interval_seconds"] == interval
    assert config["agents"]["worker"]["provider"] == "openai"
    assert config["agents"]["supervisor"]["provider"] == ("openai" if preset == "economy" else "anthropic")
    assert "interval_warning" not in config


def test_all_roles_can_be_overridden_and_short_interval_is_warned():
    assert resolve_config is not None, "configuration resolver is missing"
    config = resolve_config(overrides={"worker": "sol", "supervisor": "astra", "consultant": "fable"}, interval_s=1)
    assert config["agents"]["consultant"] == {"model": "fable", "provider": "anthropic"}
    assert config["agents"]["supervisor"]["model"] == "astra"
    assert config["agents"]["worker"]["model"] == "sol"
    assert config["interval_warning"] == "outside_spec_bounds"
    summary = render_summary(config, "Build an app")
    for text in ("Build an app", "sol", "astra", "fable", "1", "outside_spec_bounds"):
        assert text in summary


@pytest.mark.parametrize("kwargs,reason", [
    ({"preset": "brainstorm"}, "preset_requires_phase_2"),
    ({"preset": "imaginary"}, "unknown_preset"),
    ({"overrides": {"worker": "mystery"}}, "unknown_model_alias"),
    ({"interval_s": 0}, "invalid_config"),
    ({"interval_s": True}, "invalid_config"),
])
def test_invalid_configuration_is_refused(kwargs, reason):
    assert resolve_config is not None, "configuration resolver is missing"
    with pytest.raises(Refused, match=reason):
        resolve_config(**kwargs)


def test_loads_v01_json_and_applies_overrides(tmp_path):
    assert resolve_config is not None, "configuration resolver is missing"
    path = tmp_path / "config.json"
    config = {"trinity": {"version": 1, "name": "custom", "agents": {
        "worker": {"model": "luna", "provider": "openai", "execution_mode": "autonomous"},
        "supervisor": {"model": "opus", "provider": "anthropic"},
        "consultant": {"model": "astra", "provider": "openai"}},
        "checkpoints": {"mode": "hybrid", "interval_seconds": 301},
        "autonomy": {"level": "bounded", "require_approval_for_scope_changes": True}}}
    path.write_text(json.dumps(config))
    result = resolve_config(file=path, overrides={"worker": "sol"})
    assert result["name"] == "custom"
    assert result["agents"]["worker"]["model"] == "sol"
    assert result["checkpoints"]["interval_seconds"] == 301


def test_default_launch_approval_mode_is_invocation():
    assert resolve_config()["launch"]["approval_mode"] == "invocation"


def test_manual_launch_approval_mode_is_validated(tmp_path):
    path = tmp_path / "config.json"
    config = {"trinity": {"version": 1, "agents": {
        "worker": {"model": "luna"}, "supervisor": {"model": "opus"},
        "consultant": {"model": "astra"}}, "checkpoints": {"interval_seconds": 300},
        "launch": {"approval_mode": "manual"}}}
    path.write_text(json.dumps(config))
    assert resolve_config(file=path)["launch"]["approval_mode"] == "manual"
    config["trinity"]["launch"]["approval_mode"] = "automatic"
    path.write_text(json.dumps(config))
    with pytest.raises(Refused, match="invalid_config"):
        resolve_config(file=path)


@pytest.mark.parametrize("fault", ["version", "unknown", "nested", "provider", "missing", "duplicate"])
def test_config_file_rejects_unknown_keys_versions_and_malformed_shapes(tmp_path, fault):
    assert resolve_config is not None, "configuration resolver is missing"
    value = {"trinity": {"version": 1, "agents": {r: {"model": m} for r, m in zip(
        ("worker", "supervisor", "consultant"), ("luna", "opus", "astra"))},
        "checkpoints": {"interval_seconds": 300}}}
    if fault == "version": value["trinity"]["version"] = True
    if fault == "unknown": value["trinity"]["launch"] = True
    if fault == "nested": value["trinity"]["agents"]["worker"]["secret"] = "x"
    if fault == "provider": value["trinity"]["agents"]["worker"]["provider"] = "anthropic"
    if fault == "missing": del value["trinity"]["agents"]["worker"]
    path = tmp_path / "config.json"
    path.write_text('{"trinity":{},"trinity":{}}' if fault == "duplicate" else json.dumps(value))
    with pytest.raises(Refused, match="invalid_config"):
        resolve_config(file=path)
