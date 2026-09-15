"""下载富途官方文档与官方 Skills 包到 `_futu_raw/`, 供离线检索。

用途: 需要查某个接口的确切字段名/枚举值/限频规则时, 先在本地产物里 grep,
而不是靠记忆猜。文档是官方"下载 Markdown"功能的同一份内容。

用法::

    python scripts/fetch_futu_docs.py            # 文档 + Skills 包
    python scripts/fetch_futu_docs.py --docs     # 只下文档
    python scripts/fetch_futu_docs.py --skills   # 只下 Skills 包
    python scripts/fetch_futu_docs.py --lang en  # 英文文档(zh/stock_market_HK/en)

下载产物(gitignored):
    _futu_raw/Futu-API-Doc-<lang>-Python.md   官方完整接口文档
    _futu_raw/skills/                          官方 opend-skills 包解压结果
"""
from __future__ import annotations

import argparse
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # common/
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402

OUT_DIR = _PROJ / "_futu_raw"
LANG_TAG = {"zh": "zh", "hk": "hk", "en": "en"}
DOC_URL = "https://openapi.futunn.com/mds/Futu-API-Doc-{lang}-Python.md"
SKILLS_URL = "https://openapi.futunn.com/skills/opend-skills.zip"
UA = {"User-Agent": "Mozilla/5.0 (compatible; lianghua-futu-docs/0.1)"}


def fetch(url: str, timeout: int = 180) -> bytes:
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        return response.read()


def save_docs(lang: str) -> Path:
    url = DOC_URL.format(lang=LANG_TAG.get(lang, "zh"))
    data = fetch(url)
    dest = OUT_DIR / f"Futu-API-Doc-{lang}-Python.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    lines = data.count(b"\n")
    print(f"[OK] {dest}  ({len(data) / 1e6:.2f} MB, {lines} 行)")
    return dest


def save_skills() -> Path:
    data = fetch(SKILLS_URL)
    archive = OUT_DIR / "opend-skills.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    out = OUT_DIR / "skills"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        zf.extractall(out)
    print(f"[OK] {archive}  ({len(data) / 1e3:.0f} KB, {len(names)} 个文件) -> {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="zh", choices=sorted(LANG_TAG), help="文档语言")
    ap.add_argument("--docs", action="store_true", help="只下载文档")
    ap.add_argument("--skills", action="store_true", help="只下载 Skills 包")
    args = ap.parse_args()
    ensure_utf8_stdio()

    only_docs = args.docs and not args.skills
    only_skills = args.skills and not args.docs
    failed = 0
    if not only_skills:
        try:
            save_docs(args.lang)
        except Exception as exc:
            print(f"[FAIL] 文档下载失败: {type(exc).__name__}: {exc}")
            failed += 1
    if not only_docs:
        try:
            save_skills()
        except Exception as exc:
            print(f"[FAIL] Skills 包下载失败: {type(exc).__name__}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
