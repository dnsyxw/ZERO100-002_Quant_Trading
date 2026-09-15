"""富途接入配置的单元测试。"""
from __future__ import annotations

import json

import pytest

from quant_common.futu.config import FutuConfig
from quant_common.futu.errors import FutuConfigError


def write_cfg(tmp_path, payload: dict):
    path = tmp_path / "futu.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestDefaults:
    def test_缺省配置可用(self):
        cfg = FutuConfig()
        assert (cfg.host, cfg.port) == ("127.0.0.1", 11111)
        assert cfg.trd_env == "SIMULATE"
        assert cfg.enable_real_trade is False
        assert cfg.market_prefix == "SH"

    def test_仓库默认配置文件合法(self):
        cfg = FutuConfig.load()
        assert cfg.port == 11111
        assert cfg.max_order_pct <= 1
        assert cfg.default_kline_max > 0

    def test_市场前缀映射(self):
        assert FutuConfig(market="CN").market_prefix == "SH"
        assert FutuConfig(market="HK").market_prefix == "HK"
        assert FutuConfig(market="US").market_prefix == "US"


class TestLoad:
    def test_下划线键视为注释(self, tmp_path):
        path = write_cfg(tmp_path, {"_readme": ["说明", "多行"], "port": 22222})
        assert FutuConfig.load(path).port == 22222

    def test_未知字段报错(self, tmp_path):
        path = write_cfg(tmp_path, {"prot": 22222})
        with pytest.raises(FutuConfigError, match="未知字段"):
            FutuConfig.load(path)

    def test_非法JSON报错(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(FutuConfigError, match="JSON"):
            FutuConfig.load(path)

    def test_环境变量覆盖(self, tmp_path):
        path = write_cfg(tmp_path, {"port": 11111, "trd_env": "SIMULATE"})
        cfg = FutuConfig.load(path, env={"FUTU_PORT": "12345", "FUTU_TRD_ENV": "REAL",
                                         "FUTU_ENABLE_REAL_TRADE": "true"})
        assert cfg.port == 12345
        assert cfg.trd_env == "REAL"
        assert cfg.enable_real_trade is True

    def test_空环境变量不覆盖(self, tmp_path):
        path = write_cfg(tmp_path, {"port": 11111})
        assert FutuConfig.load(path, env={"FUTU_PORT": ""}).port == 11111

    def test_环境变量类型错误(self, tmp_path):
        path = write_cfg(tmp_path, {"port": 11111})
        with pytest.raises(FutuConfigError):
            FutuConfig.load(path, env={"FUTU_ENABLE_REAL_TRADE": "maybe"})

    def test_缺失文件回退默认(self, tmp_path):
        cfg = FutuConfig.load(tmp_path / "不存在.json")
        assert cfg.port == 11111


class TestValidation:
    def test_端口越界(self):
        with pytest.raises(FutuConfigError, match="port"):
            FutuConfig(port=0)
        with pytest.raises(FutuConfigError, match="port"):
            FutuConfig(port=70000)

    def test_交易环境非法(self):
        with pytest.raises(FutuConfigError, match="trd_env"):
            FutuConfig(trd_env="LIVE")

    def test_风控阈值越界(self):
        with pytest.raises(FutuConfigError, match="max_order_pct"):
            FutuConfig(max_order_pct=0)
        with pytest.raises(FutuConfigError, match="max_daily_buy_pct"):
            FutuConfig(max_daily_buy_pct=1.5)

    def test_is_simulate(self):
        assert FutuConfig(trd_env="SIMULATE").is_simulate
        assert not FutuConfig(trd_env="REAL").is_simulate


class TestPathsAndRedaction:
    def test_相对路径解析到仓库根(self):
        cfg = FutuConfig()
        assert cfg.path("runtime/futu_orders.jsonl").is_absolute()
        assert cfg.path("runtime/futu_orders.jsonl").name == "futu_orders.jsonl"

    def test_绝对路径原样返回(self, tmp_path):
        cfg = FutuConfig(log_dir=str(tmp_path))
        assert cfg.path(cfg.log_dir) == tmp_path

    def test_redacted_列出待提供凭据且不含密码(self):
        data = FutuConfig().redacted()
        assert data["trd_env"] == "SIMULATE"
        assert len(data["secrets_required"]) >= 2
        joined = json.dumps(data, ensure_ascii=False)
        assert "password" not in joined.lower() or "密码" in joined
        # 配置本身不应有 password 字段
        assert "password" not in {k.lower() for k in data}
