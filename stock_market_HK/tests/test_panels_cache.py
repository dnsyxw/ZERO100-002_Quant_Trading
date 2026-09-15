"""代码表缓存的回归测试。

守住的是一条**性能契约**: `all_cached_codes_in_range` 判断"区间内有没有行情"必须真的把
每只股票的日线读出来(2492 只约 5 分钟), 而它只是 `(start, end)` + 缓存目录内容的纯函数。
参数扫描脚本每次调用 `prepare_frames` 都会重付这 5 分钟, 因此这里做了两级缓存
(进程内 lru_cache + 磁盘, 磁盘用"文件数 + 最新 mtime"指纹失效)。

这些测试不重复读 parquet(第一次调用之后都命中缓存), 所以很快。
"""
from __future__ import annotations

import pytest

from quant_hk import panels as hkpanels
from quant_hk import store

pytestmark = pytest.mark.skipif(
    not store.list_cached_codes(),
    reason="需要港股日线缓存; 先运行 scripts/hk_download_data.py",
)

#: 一个一定能命中缓存的宽区间(覆盖全部缓存数据)
START, END = "2015-01-01", "2026-09-10"


class TestCodesInRangeCache:
    def test_重复调用结果一致(self):
        a = hkpanels.all_cached_codes_in_range(START, END)
        b = hkpanels.all_cached_codes_in_range(START, END)
        assert a == b
        assert len(a) > 0

    def test_返回升序且无重复(self):
        codes = hkpanels.all_cached_codes_in_range(START, END)
        assert codes == sorted(codes)
        assert len(codes) == len(set(codes))

    def test_返回副本而非缓存本体(self):
        """调用方改动返回值不能污染缓存 —— 这正是缓存内部存 tuple 的原因。"""
        a = hkpanels.all_cached_codes_in_range(START, END)
        a.append("99999.HK")
        b = hkpanels.all_cached_codes_in_range(START, END)
        assert "99999.HK" not in b

    def test_区间收窄结果不增加(self):
        wide = set(hkpanels.all_cached_codes_in_range(START, END))
        narrow = set(hkpanels.all_cached_codes_in_range("2021-01-01", "2021-12-31"))
        assert narrow <= wide

    def test_磁盘缓存指纹可算(self):
        """指纹只 stat 不读内容(毫秒级); 目录一变指纹就变, 缓存自动失效。"""
        fp = hkpanels._daily_dir_fingerprint()
        assert isinstance(fp, str) and ":" in fp
        n_files, newest = fp.split(":")
        assert int(n_files) == len(store.list_cached_codes())
        assert float(newest) > 0
