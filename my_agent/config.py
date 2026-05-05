"""
Central config loader. Reads my_agent/config.yaml and exposes a single
`cfg` object. Access values as cfg.agent.model, cfg.memory.enabled, etc.
"""

import yaml
from pathlib import Path
from types import SimpleNamespace


def _to_namespace(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in d.items()})
    return d


_raw = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
cfg = _to_namespace(_raw)
