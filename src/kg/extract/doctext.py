# -*- coding: utf-8 -*-
"""带页码索引的全文 —— 规则在全文上匹配，再把位置映射回"哪一页"。

**为什么必须有它**：POC 的规则是在整份文本上跑的，抽出来的关系**没有页码**；
而本项目要求"每条边带出处（文件 + 页 + 原文片段）"。
做法：把解析产物按页拼成一份全文，同时记录每段的 `[start, end) -> page`；
    正则拿到**绝对偏移**后反查页码，并截取原文片段。

⚠️ 要求：文本在抽取前若做过清洗，**必须做等长替换**（把命中的噪声换成同长度的空格），
   否则偏移全错、页码全错。见 `pending.blank_noise()`。
"""
import json
from pathlib import Path


class DocText:
    def __init__(self, doc, sep="\n"):
        self.file = doc.get("file")
        self.doc = doc
        parts, spans = [], []
        pos = 0
        for pg in (doc.get("pages") or []):
            for para in (pg.get("paras") or []):
                if not para:
                    continue
                start = pos
                parts.append(para)
                pos += len(para)
                spans.append((start, pos, pg.get("page")))
                parts.append(sep)
                pos += len(sep)
        self.text = "".join(parts)
        self.spans = spans

        # ⭐ 行级文本（与 self.text 同序，但保留"行"边界）。
        #    为什么必须有它：段落是把视觉行**无缝拼接**出来的（这对正文跨行句子是对的），
        #    但**参会名单是"每行一个单位块"的列表型内容** —— 拼接后上一行末尾的人名
        #    会和下一行开头的单位名粘在一起，被当成单位名
        #    （实测：`…肖辉、景清涛中建科工集团有限公司：…` → ORG 实体「景清涛中建科工集团有限公司」，
        #      同理还产生过 `蒋清球中南院江华试验检测项目部`）。
        #    行边界在 rows 里本来就有，这里只是**不丢掉它**。
        self.line_texts = []
        for pg in (doc.get("pages") or []):
            for r in (pg.get("rows") or []):
                t = (r.get("text") or "").strip()
                if t:
                    self.line_texts.append(t)
        self.lines_text = "\n".join(self.line_texts)

    def page_of(self, pos):
        """二分查 pos 落在哪一页。"""
        lo, hi = 0, len(self.spans) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            start, end, page = self.spans[mid]
            if pos < start:
                hi = mid - 1
            elif pos >= end:
                lo = mid + 1
            else:
                return page
        return self.spans[0][2] if self.spans else None

    def evidence(self, start, end, quote_max=160):
        """产出 evidence = {file, page, quote}（写进边的 properties 的那个结构）。"""
        quote = self.text[start:end].replace("\n", "").strip()
        if len(quote) > quote_max:
            quote = quote[:quote_max] + "…"
        return {"file": self.file, "page": self.page_of(start), "quote": quote}

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))
