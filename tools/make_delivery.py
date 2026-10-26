# -*- coding: utf-8 -*-
"""生成交付包：kg_delivery_<日期>.zip

原则（2026-10-12 与用户确认）：**交付包只含项目代码 + 配置 + 成果数据**；
环境（.venv / sem2_libs）由接手方在服务器端按 README.md §6 在线 pip 安装——
避免本机打包搬运带来的平台不兼容。

⛔ 自动排除：config/llm_key.local（密钥）、.venv、sem2_libs、__pycache__、var/parse、per_file。

用法：
    .venv\\Scripts\\python.exe tools\\make_delivery.py
"""
import os
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
STAMP = date.today().strftime("%Y%m%d")

# 顶层包含（目录整体 / 文件）
INCLUDE_DIRS = ["src", "config", "tests", "tools", "output", "deploy"]
INCLUDE_FILES = ["run.py", "README.md", "接口文档.md",
                 "requirements.txt", "requirements.lock.txt"]
# var 内只带成果与报告，不带可重算的大缓存
INCLUDE_VAR = [
    "var/extract/graph_resolved.json",
    "var/extract/graph_raw.json",
    "var/extract/pending_candidates.json",
    "var/extract/pending_filled.json",
    "var/eval",
]
EXCLUDE_NAMES = {"llm_key.local", "__pycache__", ".venv", "sem2_libs"}
EXCLUDE_SUFFIX = (".pyc", ".local", ".zip")  # ⛔ .zip 必须排除：否则把上次生成的交付包塞进自己，死循环
# ⛔ Excel/预览打开 xlsx 时会在目录里留 ~$ 锁文件（PermissionError）——一并排除
EXCLUDE_PREFIX = ("~$",)


def add_file(zf, path: Path, arcname: str):
    zf.write(path, arcname)


def add_tree(zf, src: Path, prefix: str):
    for r, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in EXCLUDE_NAMES]
        for f in files:
            if f.endswith(EXCLUDE_SUFFIX) or f in EXCLUDE_NAMES or f.startswith(EXCLUDE_PREFIX):
                continue
            full = Path(r) / f
            add_file(zf, full, str(Path(prefix) / full.relative_to(src)))


def main():
    OUT.mkdir(exist_ok=True)
    target = OUT / ("kg_delivery_%s.zip" % STAMP)
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in INCLUDE_DIRS:
            p = ROOT / d
            if p.is_dir():
                add_tree(zf, p, d)
        for f in INCLUDE_FILES:
            p = ROOT / f
            if p.is_file():
                add_file(zf, p, f)
        for rel in INCLUDE_VAR:
            p = ROOT / rel
            if p.is_dir():
                add_tree(zf, p, rel)
            elif p.is_file():
                add_file(zf, p, rel)
        # 计数
        n = sum(1 for _ in zf.infolist())
    size = target.stat().st_size / 1048576
    print("交付包：", target)
    print("文件数：%d | 大小：%.1f MB" % (n, size))
    print("⛔ 自检：包内不得出现 llm_key.local / .venv / sem2_libs / var/parse")
    bad = [i.filename for i in zipfile.ZipFile(target).infolist()
           if "llm_key.local" in i.filename or i.filename.startswith(("sem2_libs", ".venv", "kg/var/parse"))]
    print("自检结果：", "异常：" + str(bad[:5]) if bad else "干净 ✓")


if __name__ == "__main__":
    main()
