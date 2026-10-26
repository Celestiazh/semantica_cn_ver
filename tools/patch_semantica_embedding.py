# -*- coding: utf-8 -*-
"""patch_semantica_embedding.py —— 让 Semantica 的 OpenAIStore 支持 base_url（指定 embedding 服务地址）。

背景（2026-10-19）：v0.6.8 的 OpenAIStore 只收 api_key，客户端硬连 api.openai.com；
内网/私有部署的 embedding 服务（如 vLLM 部署的 qwen3-embedding）无法接入。
修复：config 支持 base_url（或环境变量 KG_EMBED_BASE_URL），透传给 OpenAI 客户端。
幂等；--undo 还原；sem2_libs 重装后需重跑本补丁。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sem2_libs" / "semantica" / "embeddings" / "provider_stores.py"
BACKUP = TARGET.with_suffix(".py.kgorig")
MARK = "# [kg-patch base-url]"

if "--undo" in sys.argv:
    if BACKUP.is_file():
        shutil.copyfile(BACKUP, TARGET)
        print("已还原：", TARGET.name)
    else:
        print("没有备份可还原")
    sys.exit(0)

if not TARGET.is_file():
    sys.exit("找不到 %s" % TARGET)
src = TARGET.read_text(encoding="utf-8")
if MARK in src:
    print("已打过补丁，跳过")
    sys.exit(0)

if not BACKUP.is_file():
    shutil.copyfile(TARGET, BACKUP)

old = """                self.client = OpenAI(api_key=self.api_key)"""
new = """                # [kg-patch base-url] 支持指定 embedding 服务地址（内网/私有部署）
                _base = config.get("base_url") or os.getenv("KG_EMBED_BASE_URL")
                self.client = OpenAI(api_key=self.api_key, base_url=_base) if _base else OpenAI(api_key=self.api_key)"""
assert old in src, "锚点未找到（client 初始化行）"
src = src.replace(old, new, 1)
TARGET.write_text(src, encoding="utf-8")
print("补丁完成：OpenAIStore 支持 base_url（备份 %s；还原 --undo）" % BACKUP.name)
