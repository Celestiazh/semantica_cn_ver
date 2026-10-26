# -*- coding: utf-8 -*-
"""test_embedding.py —— 验证 embedding 配置（settings.yaml → embeddings 段）。

用法：
    .venv\\Scripts\\python.exe tools\\test_embedding.py "中建科工参与拉普拉斯产业园建设"
输出：向量维度 + 前 3 维样例。维度非 0 即通。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sem2_libs"))

import yaml  # noqa: E402

cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
emb = cfg.get("embeddings") or {}
if not emb:
    sys.exit("settings.yaml 里没有 embeddings 段")

api_key = emb.get("api_key") or ""
if not api_key and emb.get("api_key_file"):
    kf = ROOT / emb["api_key_file"]
    if kf.is_file():
        api_key = kf.read_text(encoding="utf-8").strip()
if not api_key:
    sys.exit("没有 api_key / api_key_file —— 无法鉴权")

from semantica.embeddings import OpenAIStore  # noqa: E402  （需先跑 patch_semantica_embedding.py）

store = OpenAIStore(api_key=api_key, model=emb.get("model", ""), base_url=emb.get("base_url", ""))
text = " ".join(sys.argv[1:]) or "中建科工参与拉普拉斯产业园建设"
vec = store.embed(text)
print("服务: %s | 模型: %s" % (emb.get("base_url"), emb.get("model")))
print("文本: %s" % text)
print("向量维度: %d ｜ 前 3 维: %s" % (len(vec), [round(float(x), 4) for x in list(vec)[:3]]))
print("验证通过 ✓" if len(vec) else "失败")
