# -*- coding: utf-8 -*-
"""大模型接口层 —— 可插拔 provider，**只用标准库**（不新增依赖）

由 `settings.yaml` 的 `llm` 段一个开关切模式，**同一份代码**：

| provider            | 行为                                    | 用在 |
| :------------------ | :-------------------------------------- | :--- |
| `none`（默认）       | 不调用任何模型，第 2 层只出候选           | **Agent 模式**：Agent 自己有模型，把候选调走后自己抽 |
| `ollama`            | 调本地 Ollama `/api/chat`                | 本机测试 |
| `openai-compatible` | 调任意兼容 `/v1/chat/completions` 的服务  | ⭐ **Hy3（vLLM/SGLang）走这条** |

## 针对 Hy3（vLLM / SGLang）的三处专门处理（2026-09-28 加）

1. ⭐ **显式关思考模式**：带 `chat_template_kwargs.reasoning_effort = no_think`。
   Hy3 有 no_think/low/high 三档；不关会把输出 token 大量烧在思维链上，
   而且思维链里可能出现 JSON → 污染解析。该字段由 `llm.reasoning_effort` 控制，置空则不发送。
2. ⭐ **思维链剥离**：优先取 `message.content`；若服务端另给了 `reasoning_content`，
   只当"思考"、不参与解析；同时把 content 里的 `...` / `<thinking>…</thinking>` 去掉再找 JSON。
3. ⭐ **批量调用**：默认 20 条/次（`llm.batch_size`）。逐条发请求会把系统提示重复 N 遍
   （297 条 = 297 次），批量能显著降 token 与请求数。**批量解析失败会自动回退逐条**，不会整批丢。

## 其余设计要点

- **只填字段，不改原文**：模型只能给字段填值；`quote` 一字不改（回灌时还会再校验一次）。
- **不给就留空**：提示词明写「信息原文里没有就返回空串，**不要猜**」——
  最危险的失败模式是它硬编一个责任单位，那会把错误结论写进图里。
- **结果落缓存**：`var/extract/llm_cache.json`，键 = provider+model+原文 → 重跑不重复花钱。
- **留痕**：`filled_by = "llm:<provider>:<model>"` + 时间 + 是否走了回退。
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

FIELDS = ("responsible_org", "deadline", "deadline_iso", "matter", "confidence")

PROMPT_SYSTEM_BATCH = (
    "你是工程监理会议纪要的信息抽取器。只做抽取：不改写、不总结、不推测、不解释。\n"
    "输入是若干条带编号的原文片段。对每一条输出一个对象，字段如下：\n"
    '  "id"              : 原样带回该条前面的**编号**（就是方括号里的数字，如 "1"、"2"）\n'
    '  "responsible_org" : 该条要求的责任单位 —— **只填机构名**（如某公司/局/署/委/院/部/中心/项目部），\n'
    '                      **不要带动作、动词、事项描述**\n'
    '                      例：原文「需工务署OA提醒交警大队厨房位置设计需尽快确定」→ 填 "工务署"，\n'
    '                      不要填 "工务署OA提醒交警大队"。原文没写单位就返回空串。\n'
    '  "deadline"        : 期限 —— 原文出现「尽快/及时/抓紧/尽早/限期/即日/会后/本周/下周/周X前/\n'
    '                      某月某日前/两日内」等表述时**必须原样填入**；完全没有时间表述才返回空串。\n'
    '  "deadline_iso"    : 期限归一为 YYYY-MM-DD；不确定就返回空串\n'
    '  "matter"          : 这条要求做什么（不超过 40 字，用原文措辞，不要改写）\n'
    '  "confidence"      : 0.0~1.0\n'
    "⚠️ 三条硬要求：\n"
    "  1. **条数与输入条数必须一致，一条都不能漏、不能合并**；\n"
    "  2. **每条都要写全所有字段**，取不到就写空串 \"\"，不要省略字段；\n"
    "  3. 信息在原文里没有就返回空串，**绝对不要猜、不要编单位名**。\n"
    "📎 每条可能带一个「章节」标注（如\"一、施工单位汇报\"）。它**只是上下文参考**，"
    "**不要**因此把章节里的单位直接当成责任单位 —— 责任单位要看句子本身：**谁被要求做什么**。\n"
    '只输出 JSON：{"items": [ {...}, {...} ]}，不要任何其它文字。'
)

PROMPT_SYSTEM_ONE = (
    "你是工程监理会议纪要的信息抽取器。只做抽取：不改写、不总结、不推测、不解释。\n"
    "对给定原文片段输出 JSON，字段如下：\n"
    '  "responsible_org" : 责任单位 —— **只填机构名**（如某公司/局/署/委/院/部/中心/项目部），\n'
    '                      **不要带动作、动词、事项描述**。\n'
    '                      例：原文「需工务署OA提醒交警大队厨房位置设计需尽快确定」→ "工务署"，\n'
    '                      不要填 "工务署OA提醒交警大队"。原文没写单位就返回空串。\n'
    '  "deadline"        : 期限 —— 原文出现「尽快/及时/抓紧/尽早/限期/即日/会后/本周/下周/周X前/\n'
    '                      某月某日前/两日内」等表述时**必须原样填入**；完全没有时间表述才返回空串。\n'
    '  "deadline_iso"    : YYYY-MM-DD 或空串\n'
    '  "matter"          : 要求做什么，不超过 40 字，用原文措辞，不要改写\n'
    '  "confidence"      : 0.0\n'
    "⚠️ 可能带一个「所属章节」标注（如\"一、施工单位汇报\"）—— **只是上下文参考**，"
    "不要因此把章节里的单位当成责任单位；责任单位看句子本身（谁被要求做什么）。\n"
    "⚠️ 字段一个都不能少（取不到写空串），原文没有的绝对不要猜。只输出 JSON。"
)

_THINK_BLOCK = re.compile(r"<\s*(thinking|analysis|reasoning)\s*>.*?<\s*/\s*\1\s*>", re.S | re.I)


def strip_thinking(text):
    """把思维链从 content 里剥掉（有些服务不把它放进 reasoning_content，直接混在 content 里）。"""
    if not text:
        return ""
    t = _THINK_BLOCK.sub("", text)
    t = re.sub(r"^\s*(Thinking|思考)[^\n]*\n", "", t, flags=re.I)
    return t.strip()


def message_text(message):
    """从一条 message 里取"答案文本"。兼容 content 为字符串 / 分段数组两种形态。"""
    if not isinstance(message, dict):
        return ""
    c = message.get("content")
    if isinstance(c, list):
        return "".join(part.get("text", "") for part in c if isinstance(part, dict))
    return c or ""


class BaseProvider:
    name = "base"
    model = ""

    def available(self):
        return True

    def fill(self, items, fields=None, cache=None, on_item=None):
        """items: [{id, quote, section, rules}] → {id: {字段...}}"""
        raise NotImplementedError


class NoneProvider(BaseProvider):
    """不调模型：候选原样返回，字段留空等调用方（Agent）填。"""

    name = "none"

    def fill(self, items, fields=None, cache=None, on_item=None):
        return {}


def parse_json_payload(text):
    """从模型输出里抠 JSON。容忍 ```json 围栏、前后废话、思维链残留。"""
    t = strip_thinking(text or "")
    if not t:
        raise ValueError("空响应")
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if m:
        t = m.group(1)
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0] or [-1])
    if start < 0:
        raise ValueError("响应里没有 JSON")
    end = max(t.rfind("}"), t.rfind("]"))
    return json.loads(t[start:end + 1])


def _pick_items(obj):
    """把模型返回的 JSON 归一成 {id: fields}。"""
    if isinstance(obj, dict):
        for key in ("items", "data", "results", "list"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
        else:
            if "id" in obj or "responsible_org" in obj:
                obj = [obj]
    if not isinstance(obj, list):
        raise ValueError("JSON 结构不是数组")
    out = {}
    for i, row in enumerate(obj, 1):
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or i).strip()
        out[rid] = {k: row[k] for k in FIELDS if k in row}
    return out


def _map_back(got, batch):
    """把模型返回的 {键: 字段} 严格映射回候选真实 id。

    ⛔⛔ 这里有一条用血换来的红线（2026-09-28）：**绝不做"按顺序硬配"**。
    第一版我为了"别丢数据"加了"数量对得上就按顺序补"，结果模型抄错 id 时，
    字段被安到了**其它条目**头上 —— 实测出现 #002 两轮内容完全不同
    （"整改回复下发的安全质量监理通知单" ↔ "机电施工需单独列入周报"）。
    张冠李戴会直接产出**错误结论**，比留空严重得多。

    现在的规则：用**短序号**（1..N）做配对（模型不可能抄错 60 字的 id）；
    序号对不上又认不出真实 id 的条目 → **就是空缺**，交给 `fill()` 逐条重试。
    仅当"单条候选 + 模型只返回一个结果"时直接认领（无歧义，不算猜）。
    """
    real = {b[0] for b in batch}
    short = {str(i + 1): b[0] for i, b in enumerate(batch)}
    out = {}
    for k, v in got.items():
        kk = str(k).strip()
        if kk in short and short[kk] not in out:
            out[short[kk]] = v
        elif kk in real and kk not in out:
            out[kk] = v
    if not out and len(batch) == 1 and len(got) == 1:
        out[batch[0][0]] = next(iter(got.values()))
    return out


class _HttpProvider(BaseProvider):
    """共用 HTTP 骨架：POST JSON、重试、批量 + 失败回退逐条。"""

    def __init__(self, model="", timeout=120, batch_size=20, reasoning_effort="no_think"):
        self.model = model
        self.timeout = timeout
        self.batch_size = max(1, int(batch_size or 1))
        self.reasoning_effort = reasoning_effort
        self.retries = 2

    # ---- HTTP ----
    def _post(self, url, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            if v:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError("HTTP %s %s | %s" % (e.code, e.reason, body)) from None

    # ---- 子类实现 ----
    @staticmethod
    def _norm(batch):
        """容忍 (id, quote) 与 (id, quote, section) 两种调用形态。"""
        out = []
        for b in batch:
            out.append((b[0], b[1], b[2] if len(b) > 2 else None))
        return out

    def _chat_batch(self, batch):
        """batch: [(id, quote[, section])] → {id: fields}"""
        raise NotImplementedError

    def _chat_one(self, item):
        rid, quote = item[0], item[1]
        sec = item[2] if len(item) > 2 else None
        return self._chat_batch([(rid, quote, sec)]).get(rid) or {}

    # ---- 缓存键 ----
    def _key(self, item):
        raw = "%s|%s|%s" % (self.name, self.model, item.get("quote", ""))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    # ---- 主流程 ----
    def fill(self, items, fields=None, cache=None, on_item=None, max_workers=1):
        """max_workers>1 → 批次并发请求（vLLM 本就为并发设计；批次间无依赖）。

        线程安全：cache/out 的合并只在主线程做（worker 只返回结果）；
        _chat_batch/_post 只读实例配置，无共享可变状态。
        2026-10-10 加并发的原因：2172 条串行要 111 分钟，纯批处理排队太浪费。
        """
        out, pending = {}, []
        for it in items:
            key = self._key(it)
            if cache is not None and key in cache:
                out[it["id"]] = cache[key]
                if on_item:
                    on_item(it, cache[key], True)
            else:
                pending.append((it, key))

        size = getattr(self, "batch_size", 20)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]

        def run_batch(batch):
            """单批：重试 + 整批失败回退逐条（与原串行版逻辑一致）。"""
            payload_batch = [(it["id"], it.get("quote", ""), it.get("section"))
                             for it, _ in batch]
            got, last_err = None, None
            for attempt in range(self.retries + 1):
                try:
                    got = self._chat_batch(payload_batch)
                    last_err = None
                    break
                except Exception as e:                       # noqa: BLE001
                    last_err = "%s: %s" % (type(e).__name__, e)
                    got = None
                    if attempt < self.retries:
                        time.sleep(1.5 * (attempt + 1))
            if not got:
                # ⭐ 整批失败 → 回退逐条，别把一批候选全丢掉
                got = {}
                for it, _ in batch:
                    try:
                        one = self._chat_one((it["id"], it.get("quote", ""), it.get("section")))
                        if one:
                            got[it["id"]] = one
                    except Exception as e:                   # noqa: BLE001
                        last_err = "%s: %s" % (type(e).__name__, e)
            res = []
            for it, key in batch:
                v = dict(got.get(it["id"]) or {})
                if not v and last_err:
                    v = {"_error": last_err}
                elif v.get("_error") and len(v) > 1:
                    v.pop("_error", None)      # 既报错又拿到字段 → 留字段
                res.append((it, key, v))
            return res

        def merge(results):
            for it, key, v in results:
                out[it["id"]] = v
                if cache is not None and v and "_error" not in v:
                    cache[key] = v
                if on_item:
                    on_item(it, v, False)

        if max_workers > 1 and len(batches) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(run_batch, b): len(b) for b in batches}
                done = 0
                for fu in as_completed(futs):
                    merge(fu.result())
                    done += 1
                    print("  批次 %d/%d 完成（并发 %d）" % (done, len(batches), max_workers))
        else:
            for b in batches:
                merge(run_batch(b))
        return out


class OllamaProvider(_HttpProvider):
    name = "ollama"

    def __init__(self, base_url="http://127.0.0.1:11434", model="qwen2.5:7b",
                 timeout=120, batch_size=10):
        super().__init__(model=model or "qwen2.5:7b", timeout=timeout, batch_size=batch_size)
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")

    def _chat_batch(self, batch):
        batch = self._norm(batch)
        user = "\n\n".join("[%s] %s" % (b[0], b[1]) for b in batch)
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": PROMPT_SYSTEM_BATCH},
                         {"role": "user", "content": user}],
        }
        data = self._post(self.base_url + "/api/chat", payload)
        return _map_back(
            _pick_items(parse_json_payload(data.get("message", {}).get("content", ""))), batch)


class OpenAICompatProvider(_HttpProvider):
    """Hy3 / vLLM / SGLang / 任何兼容 /v1/chat/completions 的服务。"""

    name = "openai-compatible"

    def __init__(self, base_url="", model="", api_key="", timeout=120, batch_size=20,
                 reasoning_effort="no_think"):
        super().__init__(model=model, timeout=timeout, batch_size=batch_size,
                         reasoning_effort=reasoning_effort)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""

    def available(self):
        return bool(self.base_url and self.model)

    def _chat_batch(self, batch):
        batch = self._norm(batch)
        if not self.available():
            raise RuntimeError("openai-compatible 需要 base_url 与 model")
        # ⭐ 批量用**短序号** 1..N 喂给模型（60 字的长 id 容易被抄错，抄错就会错配）
        shorts = [str(i + 1) for i in range(len(batch))]

        def body(q, sec):
            return ("【所属章节】%s\n【原文片段】%s" % (sec, q)) if sec else q

        if len(batch) == 1:
            system = PROMPT_SYSTEM_ONE
            user = body(batch[0][1], batch[0][2])
        else:
            system = PROMPT_SYSTEM_BATCH
            user = "\n\n".join("[%s] %s" % (s, body(q, sec)) for s, (_i, q, sec) in zip(shorts, batch))
        payload = {
            "model": self.model,
            "temperature": 0,          # ⭐ 抽取要确定性：同一条两次跑必须一致
            "top_p": 1,
            "max_tokens": max(512, 320 * len(batch)),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        # ⭐ Hy3 专用：显式关思考模式（置空则不发送，兼容不认这个字段的服务）
        if self.reasoning_effort:
            payload["chat_template_kwargs"] = {"reasoning_effort": self.reasoning_effort}
        data = self._post(self.base_url + "/chat/completions", payload,
                          {"Authorization": ("Bearer " + self.api_key) if self.api_key else ""})
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("输出被 max_tokens 截断（finish_reason=length）")
        # ⚠️ 注意：该服务把思维链放在 message.reasoning（不是 reasoning_content），
        #    我们只取 content，所以不受影响；这里只把键名记下来备查。
        got = _pick_items(parse_json_payload(message_text(choice.get("message") or {})))
        return _map_back(got, batch)


def resolve_api_key(lcfg, root=None):
    """按顺序找 key：配置里的 api_key > 环境变量 > 本地文件。

    ⛔ 本地文件（默认 `config/llm_key.local`）只用来放 key，**绝不外发、绝不打印**。
       放进环境变量或这个文件，都比贴到聊天/文档里安全。
    """
    import os
    if lcfg.get("api_key"):
        return lcfg["api_key"]
    env_name = lcfg.get("api_key_env") or ""
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    rel = lcfg.get("api_key_file") or "config/llm_key.local"
    if rel:
        p = Path(rel)
        if not p.is_absolute() and root is not None:
            p = Path(root) / rel
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[0].strip()
    return ""


def build_provider(cfg):
    """按 settings.yaml 的 llm 段造 provider。默认 none（不联网）。"""
    lcfg = cfg.get("llm") or {}
    kind = (lcfg.get("provider") or "none").lower()
    timeout = lcfg.get("timeout_s") or 120
    batch = lcfg.get("batch_size") or 20
    effort = lcfg.get("reasoning_effort", "no_think")
    if kind in ("", "none", "off", "disabled"):
        return NoneProvider()
    if kind == "ollama":
        return OllamaProvider(lcfg.get("base_url") or "http://127.0.0.1:11434",
                              lcfg.get("model") or "qwen2.5:7b", timeout, batch)
    if kind in ("openai", "openai-compatible", "openai_compatible"):
        return OpenAICompatProvider(lcfg.get("base_url") or "", lcfg.get("model") or "",
                                    resolve_api_key(lcfg, cfg.root), timeout, batch, effort)
    raise ValueError("未知的 llm.provider：%s（可选 none / ollama / openai-compatible）" % kind)


def load_cache(path):
    p = Path(path)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def probe(base_url, model, api_key="", timeout=30):
    """探活：GET /v1/models。返回 (ok, 信息字符串)。**不打印 key**。"""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [m.get("id") for m in (data.get("data") or [])]
        return True, "在线；可用模型 = %s" % (ids or "（空）")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, "HTTP %s %s | %s" % (e.code, e.reason, body)
    except Exception as e:                                   # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
