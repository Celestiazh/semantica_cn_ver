# -*- coding: utf-8 -*-
"""一键启动看板：起 Semantica 服务 → 等 → 自动导入 graph_resolved.json → 对账 → 开浏览器。

解决"重启即空"的摩擦：图谱事实永远在磁盘（graph_resolved.json），
本脚本保证服务恢复后**自动把图灌回去**，双击即回到可演示状态。

逻辑：
  ① 8000 已有服务 → 跳过启动（不重复拉进程）
  ② 未启动 → 拉起（DETACHED：脚本退出服务不死）
  ③ 等就绪（最长 90 秒）
  ④ 导入：服务端图是空的才导入；已有节点则打印现状跳过（避免重复导入）
  ⑤ 对账：服务端 stats vs 本地 graph_resolved.json 的实体/边数，一致即 PASS
  ⑥ 打开浏览器（--no-browser 关掉）

用法：
    python tools/serve_dashboard.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_MANAGED = Path(r"C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe")
# sem2_libs 跟项目走（工作区内，%TEMP% 清不掉）：优先环境变量 KG_SEM_LIBS，其次 kg/sem2_libs，最后兼容旧 %TEMP% 位置
if os.environ.get("KG_SEM_LIBS"):
    SEM_LIBS = Path(os.environ["KG_SEM_LIBS"])
elif (ROOT / "sem2_libs").is_dir():
    SEM_LIBS = ROOT / "sem2_libs"
else:
    SEM_LIBS = Path(os.environ.get("TEMP", "")) / "sem2_libs"
GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
BASE = "http://127.0.0.1:8000"
LOG = Path(os.environ.get("TEMP", "")) / "kg_semantica_server.log"


def http_get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def server_up():
    try:
        http_get("/openapi.json", timeout=5)
        return True
    except Exception:
        return False


def start_server():
    if not SEM_LIBS.is_dir():
        sys.exit("找不到 %s —— 先装：pip install semantica==0.6.8 --target %s（走默认 PyPI）"
                 % (SEM_LIBS, SEM_LIBS))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SEM_LIBS)
    env["SEMANTICA_ALLOW_ANONYMOUS"] = "true"
    # ⛔ Explorer 默认 CORS/WS 白名单只有开发端口 5173 —— 不加 8000 会导致
    #    /ws/graph-updates 实时通道握手 403，前端部分功能“点了没反应”
    #    （python -m semantica.server 读的是 SEMANTICA_CORS_ORIGINS）
    env["SEMANTICA_CORS_ORIGINS"] = "http://127.0.0.1:8000,http://localhost:8000"
    env["EXPLORER_CORS_ORIGINS"] = "http://127.0.0.1:8000,http://localhost:8000"
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    with open(LOG, "w", encoding="utf-8") as logf:
        subprocess.Popen(
            [str(PY_MANAGED), "-m", "semantica.server"],
            cwd=str(ROOT.parent), env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True)
    print("服务进程已拉起（日志：%s）" % LOG)


def wait_ready(timeout=90):
    for i in range(timeout // 3):
        if server_up():
            print("服务就绪（等了约 %d 秒）" % (i * 3))
            return True
        time.sleep(3)
    return False


def graph_fingerprint():
    """图内容指纹：实体 id/name 集合 + 边数（数量相同但内容变化也能识别）。"""
    import hashlib
    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    h = hashlib.md5()
    for e in sorted(g.get("entities") or [], key=lambda x: str(x.get("id"))):
        h.update((str(e.get("id")) + "|" + str(e.get("name")) + "|" + str(e.get("type"))).encode("utf-8"))
    h.update(("edges:%d" % len(g.get("relationships") or [])).encode("utf-8"))
    return h.hexdigest()


def _sidecar():
    return GRAPH.parent / ".dashboard_fp"


def _server_pid():
    r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    for ln in (r.stdout or "").splitlines():
        if ":8000" in ln and "LISTENING" in ln.upper():
            return ln.split()[-1]
    return None


def import_graph():
    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    local = {"nodes": len(g.get("entities") or []), "edges": len(g.get("relationships") or [])}
    local_fp = graph_fingerprint()
    st = http_get("/api/graph/stats")
    if st.get("node_count", 0) > 0:
        side = _sidecar()
        try:
            disk_fp = side.read_text(encoding="utf-8").strip()
        except Exception:                                # noqa: BLE001
            disk_fp = ""
        if disk_fp == local_fp:
            print("服务端已有图且指纹一致：节点 %d / 边 %d —— 跳过导入"
                  % (st["node_count"], st["edge_count"]))
            return st
        # ⛔ 数量相同但内容变了（2026-10-13 踩过）：重启服务清空内存图再导
        print("内容指纹不一致（盘上 %s / 本地 %s）→ 重启服务重灌" % (disk_fp[:8] or "无", local_fp[:8]))
        pid = _server_pid()
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            print("已停旧服务（PID %s）" % pid)
            time.sleep(2)
            if _server_pid():
                print("⛔ 旧服务停不掉（可能被占用/权限不足）——请手动执行："
                      "netstat -ano | findstr :8000 找 PID 后 taskkill /F /PID <pid>，再重跑本脚本")
                return st
        else:
            print("⛔ 未找到 8000 端口监听进程但服务仍有响应，请手动重启服务")
            return st
        start_server()
        if not wait_ready():
            print("⛔ 服务重启后未就绪，请稍后手动重跑本脚本")
            return st
        st = http_get("/api/graph/stats") or {}
    cp = subprocess.run(["curl.exe", "-s", "-m", "300", "-X", "POST", BASE + "/api/import",
                         "-F", "file=@" + str(GRAPH)],
                        capture_output=True, text=True, timeout=360)
    print("导入应答：", (cp.stdout or cp.stderr)[:200])
    if '"status":"success"' in (cp.stdout or ""):
        _sidecar().write_text(local_fp, encoding="utf-8")
    st = http_get("/api/graph/stats")
    ok = st.get("node_count") == local["nodes"] and st.get("edge_count") == local["edges"]
    print("对账：%s ｜ 本地 %s vs 服务端 %s"
          % ("PASS ✓" if ok else "FAIL ⛔", local,
             {"nodes": st.get("node_count"), "edges": st.get("edge_count")}))
    if not ok:
        sys.exit(1)
    return st


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not GRAPH.is_file():
        sys.exit("找不到 %s —— 先跑 --extract / --resolve --apply" % GRAPH)

    if server_up():
        print("服务已在运行，跳过启动")
    else:
        start_server()
        if not wait_ready():
            sys.exit("服务 90 秒未就绪，看日志：%s" % LOG)
    st = import_graph()
    print("看板地址：%s" % BASE)
    if "--no-browser" not in sys.argv:
        import webbrowser
        webbrowser.open(BASE)
    print("完成。服务在后台独立运行；要停：taskkill /IM python.exe 不行（会误伤），"
          "用 netstat -ano | findstr :8000 找 PID 后 taskkill /PID <pid> /F")


if __name__ == "__main__":
    main()
