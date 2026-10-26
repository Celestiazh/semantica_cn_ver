# -*- coding: utf-8 -*-
"""CSV/xlsx 通用读表 —— 人工确认文件的编码与格式容错。

踩过的坑（2026-09-30）：
  1. 人工用 Excel 填完"另存为"普通 CSV → 文件变 **GBK/ANSI**（utf-8 读必炸）；
  2. Excel 保存时选"保留工作簿"→ .csv 路径下装的是 **xlsx（zip）**；
  3. openpyxl 按扩展名校验 → .csv 后缀的 xlsx 会被拒，需拷到临时 .xlsx 再读。

read_rows(path) 统一处理：utf-8-sig → gbk → xlsx，返回 [dict]。
"""
import csv
import os
import tempfile


def read_rows(path):
    """读表格文件为 [dict]（utf-8-sig / gbk / xlsx 自动兼容）。"""
    path = str(path)
    # xlsx 探测：PK 头 → 拷到临时 .xlsx 用 openpyxl 读
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":
        import openpyxl
        tmp = os.path.join(tempfile.gettempdir(), "kg_confirm_tmp.xlsx")
        open(tmp, "wb").close()
        open(tmp, "wb").write(open(path, "rb").read())
        wb = openpyxl.load_workbook(tmp, read_only=True, data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(c or "").strip() for c in rows[0]]
        out = []
        for r in rows[1:]:
            out.append({h: (str(r[i]) if i < len(r) and r[i] is not None else "")
                        for i, h in enumerate(header)})
        return out
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码 %s（试过 utf-8-sig/gbk）" % path)
