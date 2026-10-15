"""Logical role configuration only; this module never launches a model."""

import copy
import json
import math
from pathlib import Path

from .coordinator import Refused


ALIASES = {"luna": "openai", "sol": "openai", "astra": "openai", "opus": "anthropic", "fable": "anthropic"}
ROLES = ("worker", "supervisor", "consultant")
PRESETS = {
    "economy": (("luna", "sol", "astra"), 900),
    "balanced": (("luna", "opus", "astra"), 300),
    "intensive": (("sol", "opus", "astra"), 180),
    "research": (("luna", "opus", "astra"), 600),
}


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise Refused("invalid_config")


def model(alias):
    if not isinstance(alias, str) or alias not in ALIASES:
        raise Refused("unknown_model_alias")
    return {"model": alias, "provider": ALIASES[alias]}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate(config):
    keys(config, ("version", "name", "agents", "checkpoints", "autonomy", "execution", "escalation", "launch"), ("version", "agents", "checkpoints"))
    if type(config["version"]) is not int or config["version"] != 1:
        raise Refused("invalid_config")
    if "name" in config and (not isinstance(config["name"], str) or not config["name"].strip()):
        raise Refused("invalid_config")
    keys(config["agents"], ROLES, ROLES)
    for role, agent in config["agents"].items():
        keys(agent, ("model", "provider", "execution_mode"), ("model",))
        resolved = model(agent["model"])
        if "provider" in agent and agent["provider"] != resolved["provider"]:
            raise Refused("invalid_config")
        if "execution_mode" in agent and (not isinstance(agent["execution_mode"], str) or not agent["execution_mode"].strip()):
            raise Refused("invalid_config")
        agent["provider"] = resolved["provider"]
    checkpoints = config["checkpoints"]
    keys(checkpoints, ("mode", "interval_seconds", "min_interval_seconds", "max_interval_seconds", "trigger_on"), ("interval_seconds",))
    for key in ("interval_seconds", "min_interval_seconds", "max_interval_seconds"):
        if key in checkpoints and (type(checkpoints[key]) not in (int, float) or not math.isfinite(checkpoints[key]) or checkpoints[key] <= 0):
            raise Refused("invalid_config")
    if "mode" in checkpoints and checkpoints["mode"] not in ("hybrid", "scheduled"):
        raise Refused("invalid_config")
    if "trigger_on" in checkpoints and (not isinstance(checkpoints["trigger_on"], list) or any(
        not isinstance(v, str) or not v for v in checkpoints["trigger_on"]
    )):
        raise Refused("invalid_config")
    sections = {
        "autonomy": {"level", "allow_implementation_changes", "allow_architectural_changes", "require_approval_for_scope_changes", "require_approval_for_external_actions"},
        "execution": {"cooperative_pause", "atomic_work_units", "preserve_sessions", "persist_state"},
        "escalation": {"consultant_can_require_replan", "supervisor_owns_execution", "user_owns_objectives"},
    }
    for name, allowed in sections.items():
        if name not in config:
            continue
        keys(config[name], allowed)
        for key, value in config[name].items():
            if name == "autonomy" and key == "level":
                if value not in ("conservative", "bounded", "high"):
                    raise Refused("invalid_config")
            elif type(value) is not bool:
                raise Refused("invalid_config")
    if "launch" in config:
        keys(config["launch"], ("approval_mode",))
        if config["launch"].get("approval_mode", "invocation") not in ("invocation", "manual"):
            raise Refused("invalid_config")
    return config


def resolve_config(preset="balanced", *, overrides=None, interval_s=None, file=None):
    if preset == "brainstorm":
        raise Refused("preset_requires_phase_2")
    if preset not in PRESETS:
        raise Refused("unknown_preset")
    if file is not None:
        try:
            document = json.loads(Path(file).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            keys(document, ("trinity",), ("trinity",))
            config = validate(document["trinity"])
        except (OSError, ValueError, TypeError) as error:
            raise Refused("invalid_config") from error
    else:
        models, interval = PRESETS[preset]
        config = {"version": 1, "name": preset,
                  "agents": {r: model(m) for r, m in zip(ROLES, models)},
                  "checkpoints": {"mode": "hybrid", "interval_seconds": interval},
                  "autonomy": {"level": "bounded"}}
    config = copy.deepcopy(config)
    config.setdefault("name", "custom")
    config.setdefault("autonomy", {"level": "bounded"})
    config.setdefault("execution", {})
    config.setdefault("escalation", {})
    config.setdefault("launch", {"approval_mode": "invocation"})
    config["launch"].setdefault("approval_mode", "invocation")
    if overrides is not None:
        keys(overrides, ROLES)
        for role, alias in overrides.items():
            config["agents"][role].update(model(alias))
    if interval_s is not None:
        config["checkpoints"]["interval_seconds"] = interval_s
    validate(config)
    if not 120 <= config["checkpoints"]["interval_seconds"] <= 900:
        config["interval_warning"] = "outside_spec_bounds"
    return config


def render_summary(config, objective):
    lines = ["Project:", objective, "Recommended configuration:"]
    lines.extend(f"{role.title()}: {config['agents'][role]['model']} ({config['agents'][role]['provider']})" for role in ROLES)
    lines.extend([f"Mode: {config['name']}", f"Autonomy: {config['autonomy'].get('level', 'bounded')}",
                  f"Checkpoints: {config['checkpoints']['interval_seconds']} seconds",
                  "State: configured", "Start requires a user caller: start --user-approved"])
    if "interval_warning" in config:
        lines.append(f"interval_warning: {config['interval_warning']}")
    return "\n".join(lines)


def adjust_config(current, *, preset=None, overrides=None, interval_s=None):
    """Apply a user's adjustment to an existing proposal's configuration."""
    if preset is not None:
        return resolve_config(preset, overrides=overrides, interval_s=interval_s)
    config = copy.deepcopy(current)
    config.pop("interval_warning", None)
    if overrides:
        keys(overrides, ROLES)
        for role, alias in overrides.items():
            config["agents"][role] = {**config["agents"][role], **model(alias)}
    if interval_s is not None:
        config["checkpoints"]["interval_seconds"] = interval_s
    validate(config)
    if not 120 <= config["checkpoints"]["interval_seconds"] <= 900:
        config["interval_warning"] = "outside_spec_bounds"
    return config
