# -*- coding: utf-8 -*-
"""Explorer 界面汉化补丁：把 UI 框架文案（英文）替换为中文。

背景（2026-10-12）：Semantica Explorer 无官方 i18n；图内容（节点名/边类型/原文）
本就是中文，英文残留只有 UI 框架文案、类型代码（ORG/PERSON…）与部分属性键。
本补丁与 patch_semantica_ui.py 同机制：往 static/index.html 注入 MutationObserver
脚本，按"可见文字精确匹配"替换（词表在 config/ui_i18n.json）。

特点：
  - 不动 2.2MB 压缩 bundle；词表改完重跑本脚本 + 刷新页面即生效
  - 可逆：首次运行备份为 index.html.i18n.orig；--undo 还原
  - 幂等：已注入则跳过
  - 兼容：先跑 patch_semantica_ui.py（隐藏入口）再跑本脚本；顺序无关但备份链各自独立

用法：
    python tools/ui_i18n_patch.py                # 用默认 static（kg/sem2_libs）
    python tools/ui_i18n_patch.py <static目录>    # 指定其它 static 目录
    python tools/ui_i18n_patch.py --undo         # 还原
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "kg-i18n-patch"
DICT_PATH = ROOT / "config" / "ui_i18n.json"
DEFAULT_STATIC = ROOT / "sem2_libs" / "semantica" / "static"

INJECT_TEMPLATE = """<style id="__MARKER___CSS">
/* 本项目补丁 CSS：隐藏确认无用的组件（词表 config/ui_i18n.json → css 键） */
__CSS__
</style>
<script id="__MARKER__">
// 本项目补丁：Explorer 界面汉化（词表见 kg/config/ui_i18n.json）。
// 由 kg/tools/ui_i18n_patch.py 注入；重装 semantica 后重跑该脚本即可恢复。
(function () {
  var TEXT = __TEXT__;
  var SUB  = __SUB__;
  var PH   = __PH__;
  var HIDE = __HIDE__;
  // 自动加"全大写"变体（CSS 转 uppercase 的徽章，DOM 里常是大写文本）
  Object.keys(TEXT).forEach(function (k) {
    var u = k.toUpperCase();
    if (u !== k && !TEXT[u]) TEXT[u] = TEXT[k];
  });
  var SUB_KEYS = Object.keys(SUB).map(function (k) {
    return { re: new RegExp(k.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"), "g"), v: SUB[k] };
  });
  function sweep(root) {
    // 1) 元素级：整段文字精确命中 → 整体替换 / 隐藏
    var nodes = (root || document).querySelectorAll("a,button,span,div,li,td,th,h1,h2,h3,h4,h5,label,p,[role='menuitem'],[role='tab'],[role='button'],[role='option']");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.dataset && el.dataset.kgI18n) continue;
      var t = (el.textContent || "").trim();
      if (!t) continue;
      if (HIDE.indexOf(t) >= 0 && el.children.length <= 1) {
        el.style.display = "none"; el.dataset.kgI18n = "1"; continue;
      }
      if (el.children.length === 0 && TEXT.hasOwnProperty(t)) {
        el.textContent = TEXT[t];
        el.dataset.kgI18n = "1";
      }
    }
    // 2) 文本节点级：精确替换（保留首尾空白），否则做子串替换
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var hits = [];
    while (w.nextNode()) {
      var n = w.currentNode;
      var par = n.parentNode;
      if (!par || /^(SCRIPT|STYLE|CANVAS|TEXTAREA)$/.test(par.tagName || "")) continue;
      var v = n.nodeValue;
      if (!v || !v.trim()) continue;
      var tv = v.trim();
      if (TEXT.hasOwnProperty(tv)) {
        var lead = v.slice(0, v.indexOf(tv));
        var tail = v.slice(v.indexOf(tv) + tv.length);
        n.nodeValue = lead + TEXT[tv] + tail;
        continue;
      }
      for (var s = 0; s < SUB_KEYS.length; s++) {
        if (SUB_KEYS[s].re.test(v)) { hits.push([n, s]); break; }
      }
    }
    for (var h = 0; h < hits.length; h++) {
      var nn = hits[h][0], sk = hits[h][1];
      if (nn.nodeValue) nn.nodeValue = nn.nodeValue.replace(SUB_KEYS[sk].re, SUB_KEYS[sk].v);
    }
    // 3) placeholder
    var ph = document.querySelectorAll("[placeholder]");
    for (var j = 0; j < ph.length; j++) {
      var p = ph[j];
      if (p.dataset.kgI18nPh) continue;
      var pv = p.getAttribute("placeholder") || "";
      if (PH.hasOwnProperty(pv)) { p.setAttribute("placeholder", PH[pv]); p.dataset.kgI18nPh = "1"; }
    }
  }
  var timer = null;
  function schedule() {
    if (timer) return;
    timer = setTimeout(function () { timer = null; sweep(document); }, 200);
  }
  function start() {
    sweep(document);
    var mo = new MutationObserver(schedule);
    mo.observe(document.body, { childList: true, subtree: true, characterData: true });
    var ticks = 0;                                   // 前 90 秒兜底轮询，防漏渲染时机
    var iv = setInterval(function () {
      sweep(document);
      if (++ticks > 45) clearInterval(iv);
    }, 2000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }
})();
</script>"""


def build_inject(d):
    return (INJECT_TEMPLATE
            .replace("__MARKER__", MARKER)
            .replace("__TEXT__", json.dumps(d.get("text", {}), ensure_ascii=False))
            .replace("__SUB__", json.dumps(d.get("sub", {}), ensure_ascii=False))
            .replace("__PH__", json.dumps(d.get("placeholder", {}), ensure_ascii=False))
            .replace("__HIDE__", json.dumps(d.get("hide", []), ensure_ascii=False))
            .replace("__CSS__", d.get("css", "")))


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    static_dir = Path(argv[0]) if argv else DEFAULT_STATIC
    index = static_dir / "index.html"
    backup = static_dir / "index.html.i18n.orig"

    if not DICT_PATH.is_file():
        sys.exit("找不到词表 %s" % DICT_PATH)
    if not index.is_file():
        sys.exit("找不到 %s —— 传对静态目录，或先装好 semantica" % index)

    if "--undo" in sys.argv:
        if backup.is_file():
            shutil.copyfile(backup, index)
            print("已还原：", index)
        else:
            print("没有备份可还原（%s 不存在）" % backup)
        return

    d = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    html = index.read_text(encoding="utf-8")
    if MARKER in html:
        print("已打过补丁，跳过（改词后想刷新注入请先 --undo 再跑）：", index)
        return
    if not backup.is_file():
        shutil.copyfile(index, backup)
        print("已备份原文件 →", backup.name)
    patched = html.replace("</body>", build_inject(d) + "\n  </body>")
    if patched == html:
        sys.exit("index.html 里找不到 </body>，格式变了？人工看一下")
    index.write_text(patched, encoding="utf-8")
    n_text = len(d.get("text", {}))
    print("已注入汉化补丁（%d 条替换 / %d 条隐藏）：" % (n_text, len(d.get("hide", []))), index)
    print("浏览器刷新即生效；还原：--undo")


if __name__ == "__main__":
    main()
