"""Configuration management for branch protection migration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).parent / "default_config.yaml"


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML file, merged with defaults.

    Priority: CLI args > env vars > user config > default config.
    """
    # Load defaults
    with open(DEFAULT_CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)

    # Overlay user config if provided
    if config_path:
        with open(config_path, "r") as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, user_config)

    # Override with environment variables
    _apply_env_overrides(config)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config: dict) -> None:
    """Apply environment variable overrides."""
    env_map = {
        "GITLAB_URL": ("gitlab", "url"),
        "GITLAB_TOKEN": ("gitlab", "token"),
        "GITHUB_TOKEN": ("github", "token"),
        "GITHUB_ORG": ("github", "org"),
        "GITHUB_API_URL": ("github", "api_url"),
    }
    for env_var, keys in env_map.items():
        value = os.environ.get(env_var)
        if value:
            obj = config
            for k in keys[:-1]:
                obj = obj.setdefault(k, {})
            obj[keys[-1]] = value
