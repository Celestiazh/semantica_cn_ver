# -*- coding: utf-8 -*-
"""屏蔽 Semantica Explorer 里我们不用的入口：Entity Resolution / Provenance。

背景（2026-09-29）：项目有**自己的消解流程**（roster + 人工/LLM CSV，沉淀进配置），
图谱导入前已归并完毕；Provenance 端点本项目也不用（溯源在边的 evidence 上）。
这两个页面留在演示界面里只会引来"这功能你们用了吗"的追问。

做法：往 `static/index.html` 注入一段小脚本（MutationObserver 按**文字精确匹配**
隐藏导航入口）。不动 2.2MB 的压缩 bundle —— 升级/重装后**重跑本脚本**即可恢复。
可逆：首次运行备份为 `index.html.orig`；幂等：已注入则跳过。

用法：
    python tools/patch_semantica_ui.py              # 补丁默认安装（%TEMP%\\sem2_libs）
    python tools/patch_semantica_ui.py <static目录>  # 指定其它 static 目录
    python tools/patch_semantica_ui.py --undo       # 还原备份
"""
import json
import shutil
import sys
from pathlib import Path

MARKER = "patched-by-kg-project"
DEFAULT_STATIC = Path.home() / "AppData" / "Local" / "Temp" / "sem2_libs" / "semantica" / "static"
HIDE_LABELS = ["Entity Resolution", "Provenance", "Provenance Dossier"]

INJECT_TEMPLATE = """<script id="__MARKER__">
// 本项目补丁：隐藏 Entity Resolution / Provenance 入口（项目有自己的消解与溯源）。
// 由 kg/tools/patch_semantica_ui.py 注入；重装 semantica 后重跑该脚本即可恢复。
(function () {
  var HIDE = __LABELS__;
  function sweep(root) {
    var nodes = (root || document).querySelectorAll(
      "a,button,[role='menuitem'],[role='tab'],li,span,div");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.dataset && el.dataset.kgHidden) continue;
      // 只隐藏"文字恰好等于目标"的叶子级元素，避免误伤整块布局
      if (el.children.length <= 1 && HIDE.indexOf((el.textContent || "").trim()) >= 0) {
        el.style.display = "none";
        el.dataset.kgHidden = "1";
      }
    }
  }
  var mo = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      if (muts[i].addedNodes.length) { sweep(document); break; }
    }
  });
  function start() {
    sweep(document);
    mo.observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }
})();
</script>"""


def build_inject():
    return (INJECT_TEMPLATE
            .replace("__MARKER__", MARKER)
            .replace("__LABELS__", json.dumps(HIDE_LABELS, ensure_ascii=False)))


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    static_dir = Path(argv[0]) if argv else DEFAULT_STATIC
    index = static_dir / "index.html"
    backup = static_dir / "index.html.orig"

    if not index.is_file():
        sys.exit("找不到 %s —— 传对静态目录，或先装好 semantica" % index)

    if "--undo" in sys.argv:
        if backup.is_file():
            shutil.copyfile(backup, index)
            print("已还原：", index)
        else:
            print("没有备份可还原（%s 不存在）" % backup)
        return

    html = index.read_text(encoding="utf-8")
    if MARKER in html:
        print("已打过补丁，跳过：", index)
        return
    if not backup.is_file():
        shutil.copyfile(index, backup)
        print("已备份原文件 →", backup.name)
    patched = html.replace("</body>", build_inject() + "\n  </body>")
    if patched == html:
        sys.exit("index.html 里找不到 </body>，格式变了？人工看一下")
    index.write_text(patched, encoding="utf-8")
    print("已注入补丁：", index)
    print("浏览器刷新即生效（服务端每次请求现读该文件）；还原：--undo")


if __name__ == "__main__":
    main()
