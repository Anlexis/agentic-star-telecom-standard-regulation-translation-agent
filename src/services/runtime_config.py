"""AgentCore Platform v1.0"""

# TEL-C2-008 — runtime configuration loader.
#
# config/config.yaml holds every runtime parameter (max_retry, timeout_s and the
# whole `translation` block). config/agent.yaml is the static manifest and holds
# registry and identity keys only — it has no runtime section, so a reader that
# looks for one there gets an empty mapping and the agent runs on hard-coded
# defaults with every declared value ignored.
#
# The loader fails LOUDLY: the file ships with the repository, so its absence or
# malformation is a broken deployment.

from __future__ import annotations

import os
from typing import Any, Dict

from framework.errors import ConfigError

# config/config.yaml at the repository root (three levels up from this file:
# src/services/runtime_config.py -> src/services -> src -> <repo root>).
RUNTIME_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "config.yaml",
)


def load_runtime_config() -> Dict[str, Any]:
    """Load and return config/config.yaml as a mapping.

    Raises ConfigError when the file is missing, unreadable, not valid YAML, or
    not a mapping.
    """
    import yaml

    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigError(
            f"runtime config not found at {RUNTIME_CONFIG_PATH}; config/config.yaml must ship with the deployment"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"runtime config could not be read: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"runtime config is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("runtime config must be a YAML mapping")
    return loaded


def translation_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the declared `translation` block of *config*.

    Raises ConfigError when the key is present but is not a mapping, so a
    malformed declaration fails loudly instead of resolving to defaults.
    """
    block = config.get("translation", {})
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError("translation must be a mapping")
    return block
