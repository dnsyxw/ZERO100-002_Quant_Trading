"""配置的读写: JSON <-> `backtest.StrategySpec`。

配置文件里**只放策略自由度**, 不放任何账号/密钥。四个 dataclass 的字段名即为
JSON 的 key, 未知字段直接报错(而不是静默忽略) —— 静默忽略会让"我改了参数但
结果没变"这种事故难以发现(本程序开发期就踩过一次同类问题)。
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from quant_global.allocate import AllocConfig
from quant_global.backtest import StrategySpec
from quant_global.engine import EngineConfig, GovernorConfig
from quant_global.signals import TrendConfig

__all__ = ["load_spec", "save_spec", "spec_to_dict", "DEFAULT_CONFIG"]

#: 默认推荐配置(见 `docs/12` §推荐配置)。改这里等于改全项目的默认值。
DEFAULT_CONFIG = "stock_market_GLOBAL/config/gtaa_default.json"


def _build(cls, raw: dict[str, Any]):
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{cls.__name__} 不认识的字段: {sorted(unknown)}; 可用: {sorted(allowed)}")
    kw = dict(raw)
    for key in ("lookbacks", "tiers", "scale_bounds"):
        if key in kw and kw[key] is not None:
            kw[key] = tuple(tuple(x) if isinstance(x, list) else x for x in kw[key])
    return cls(**kw)


def spec_to_dict(spec: StrategySpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "trend": asdict(spec.trend),
        "alloc": asdict(spec.alloc),
        "engine": asdict(spec.engine),
        "governor": asdict(spec.governor),
    }


def load_spec(path: str | Path) -> StrategySpec:
    """从 JSON 读一份策略配置。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"找不到配置文件: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    return StrategySpec(
        name=str(raw.get("name", "gtaa")),
        trend=_build(TrendConfig, raw.get("trend", {})),
        alloc=_build(AllocConfig, raw.get("alloc", {})),
        engine=_build(EngineConfig, raw.get("engine", {})),
        governor=_build(GovernorConfig, raw.get("governor", {})),
    )


def save_spec(spec: StrategySpec, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spec_to_dict(spec), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p
