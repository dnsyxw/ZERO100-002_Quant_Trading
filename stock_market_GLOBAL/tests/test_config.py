"""配置读写与篮子定义的测试。"""
from __future__ import annotations

import json

import pytest

from quant_global import config as gcfg
from quant_global import universe
from quant_global.backtest import StrategySpec


def test_默认配置文件存在且能被加载():
    spec = gcfg.load_spec(gcfg.DEFAULT_CONFIG)
    assert isinstance(spec, StrategySpec)
    assert 0 < spec.alloc.target_vol <= 1
    assert spec.alloc.gross_max >= 1.0
    assert spec.alloc.allow_short is False, "默认必须是不做空"


def test_配置往返一致(tmp_path):
    spec = gcfg.load_spec(gcfg.DEFAULT_CONFIG)
    p = gcfg.save_spec(spec, tmp_path / "round.json")
    assert gcfg.load_spec(p) == spec


def test_未知字段直接报错而不是静默忽略(tmp_path):
    raw = json.loads((tmp := __import__("pathlib").Path(gcfg.DEFAULT_CONFIG))
                     .read_text(encoding="utf-8"))
    raw["alloc"]["target_volat"] = 0.2      # 拼错
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="不认识"):
        gcfg.load_spec(p)


def test_配置文件里不许出现账号或密钥字样():
    """配置文件会被提交进 git —— 里面绝不能有凭据。"""
    text = __import__("pathlib").Path(gcfg.DEFAULT_CONFIG).read_text(encoding="utf-8")
    for bad in ("acc_id", "password", "token", "secret", "key"):
        assert bad not in text.lower().replace("_readme", "")


def test_核心篮子不含晚上市的标的():
    """篮子选择规则是机械的: 只收 2007 年前成立的最大流动性 ETF(这样才有 2008)。"""
    late = {"EDV", "PDBC", "IBIT", "GBTC", "SMH", "MCHI"}
    assert not (set(universe.symbols("core")) & late)


def test_现金腿不在交易篮子里():
    for kind in ("core", "extended"):
        assert universe.CASH_PROXY not in universe.symbols(kind)


def test_扩展篮子包含核心篮子():
    core = set(universe.symbols("core"))
    assert core <= set(universe.symbols("extended"))


def test_篮子里没有重复标的():
    syms = universe.symbols("extended")
    assert len(syms) == len(set(syms))


def test_每只标的都有资产类别与成本假设():
    for a in universe.basket("extended"):
        assert a.asset_class, a.symbol
        assert a.cost_bp > 0, a.symbol
        assert a.name, a.symbol


def test_未知篮子名报错():
    with pytest.raises(ValueError):
        universe.basket("nope")


def test_核心篮子覆盖多个资产类别():
    """多资产是本策略的核心主张 —— 如果篮子退化成"一堆股票", 分散度就没了。"""
    cls = universe.classes("core")
    assert len(cls) >= 6, f"资产类别太少: {cls}"
    assert "利率债" in cls and "贵金属" in cls and "商品" in cls
