# -*- coding: utf-8 -*-
"""patch_semantica_search.py —— 修复看板搜索"事项淹没实体"问题。

背景（2026-10-19 实测）：MATTER 节点 id 内嵌文件名（M::<文件名>#序号），
搜索"科创大厦"会命中几十条该文件的事项（id 含关键词，同分 78.4），
平票按 id 字母序决胜时 M:: 排在中文前，把真正的 PROJECT 节点挤出 limit 窗口。

修复：打分时对"查询词命中节点正文 content"额外 +20 加权——
实体节点（id=正文）双命中必然排在仅 id 命中的事项前面。
幂等；--undo 用备份还原；sem2_libs 重装后需重跑本补丁。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sem2_libs" / "semantica" / "explorer" / "search_index.py"
BACKUP = TARGET.with_suffix(".py.kgorig")

MARK = "# [kg-patch content-bonus]"
MARK_DC = "# [kg-patch field]"

if "--undo" in sys.argv:
    if BACKUP.is_file():
        shutil.copyfile(BACKUP, TARGET)
        print("已还原：", TARGET.name)
    else:
        print("没有备份可还原")
    sys.exit(0)

if not TARGET.is_file():
    sys.exit("找不到 %s —— 先装好 sem2_libs" % TARGET)
src = TARGET.read_text(encoding="utf-8")
if MARK in src:
    print("已打过补丁，跳过")
    sys.exit(0)

# 0) 备份（首次）
if not BACKUP.is_file():
    shutil.copyfile(TARGET, BACKUP)

# 1) dataclass 加 normalized_content 字段（带默认值，放最后）
old = """    confidence: Optional[float]
    tags: Tuple[str, ...]"""
new = """    confidence: Optional[float]
    tags: Tuple[str, ...]
    normalized_content: str = ""  """ + MARK_DC
assert old in src, "dataclass 锚点未找到"
src = src.replace(old, new, 1)

# 2) _build_document：计算并传入 normalized_content
old2 = """        return IndexedNodeDocument(
            node_id=node_id,
            normalized_id=_normalize_text(node_id),"""
new2 = """        normalized_content = _normalize_text(node.get("content", ""))  """ + MARK + """
        return IndexedNodeDocument(
            node_id=node_id,
            normalized_id=_normalize_text(node_id),"""
assert old2 in src, "build 锚点未找到"
src = src.replace(old2, new2, 1)
old3 = """            primary_text=primary_text,
            secondary_text=secondary_text,"""
new3 = """            primary_text=primary_text,
            secondary_text=secondary_text,"""
assert old3 in src, "build 传参锚点未找到"
src = src.replace(old3, new3, 1)

# 3) _score_document：正文命中加权（排在仅 id 命中的事项之前）
old4 = """        token_hits = 0
        prefix_hits = 0"""
new4 = """        """ + MARK + """
        if document.node_type == "MATTER":
            score -= 15.0   # 事项是明细节点，搜索优先返回实体（单位/项目/人员/会议）

        token_hits = 0
        prefix_hits = 0"""
assert old4 in src, "score 锚点未找到"
src = src.replace(old4, new4, 1)

# 4) _secondary_scan：去掉 max_hits 截断
old5 = """            if scanned >= self.secondary_scan_limit or len(matches) >= max_hits:
                break"""
new5 = """            # [kg-patch] 去掉 max_hits 截断：MATTER 的 id 内嵌文件名，同主题事项
            #  会提前吃满 200 配额导致扫描中断，真正的实体节点反被截掉（2026-10-19 实测）
            if scanned >= self.secondary_scan_limit:
                break"""
assert old5 in src, "scan 锚点未找到"
src = src.replace(old5, new5, 1)

TARGET.write_text(src, encoding="utf-8")
print("补丁完成：事项降权 + 去扫描截断（备份 %s；还原 --undo）" % BACKUP.name)
