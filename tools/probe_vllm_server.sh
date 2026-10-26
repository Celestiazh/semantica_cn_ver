#!/usr/bin/env bash
# 查 vLLM / SGLang 服务的"真正生效参数"。默认查 8007（Hy3）。
#
#   bash probe_vllm_server.sh              # 查 8007
#   bash probe_vllm_server.sh 8000         # 查别的端口
#   KEY=你的key bash probe_vllm_server.sh  # 顺带探活与拉指标
#
# 只读，不改任何东西；命令行里的 --api-key / token 会打码。
#
# ⛔ 教训（2026-09-28 踩过）：别用 `tr '\0' '\n' | grep '^--'` ——
#    cmdline 是 NUL 分隔，转成换行后**参数名和它的值会分到两行**，
#    grep '^--' 只剩下参数名，值全丢。正确做法是先把 NUL 转成空格，
#    再在 " --" 处断行。

set -u
PORT="${1:-8007}"
BASE="${BASE:-http://127.0.0.1:${PORT}}"
KEY="${KEY:-}"

hr() { echo; echo "======== $* ========"; }
mask() { sed -E 's/((--?api[-_]?key|--?token|secret)[= ]+)[^ ]+/\1***MASKED***/g'; }
# ⭐ 正确打印启动参数：NUL→空格，再在 " --" 前断行，参数名与值始终在同一行
show_cmd() {
  tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | sed 's/ --/\n    --/g' | sed 's/^/    /' | mask
}

hr "0. 谁在监听 ${PORT}"
(ss -ltnp 2>/dev/null | grep ":${PORT}\b" || netstat -ltnp 2>/dev/null | grep ":${PORT}\b") | cut -c1-140
PID=$( (ss -ltnp 2>/dev/null | grep ":${PORT}\b" | grep -oP 'pid=\K[0-9]+' | head -1) )
[ -z "$PID" ] && PID=$(lsof -ti:"${PORT}" 2>/dev/null | head -1)
[ -z "$PID" ] && PID=$(fuser "${PORT}/tcp" 2>/dev/null | tr -d ' ')
CONTAINER=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -w "${PORT}" | awk '{print $1}' | head -1)
echo "PID=${PID:-（没找到）}  容器=${CONTAINER:-（无）}"

hr "1. ⭐ 完整启动参数（参数名 + 值）"
if [ -n "$PID" ] && [ -r "/proc/$PID/cmdline" ]; then
  show_cmd "$PID"
else
  echo "  端口进程拿不到，退一步：列出所有 GPU 进程的完整命令行（自己认哪组是 Hy3）"
  for P in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' '); do
    echo "  --- pid $P ---"
    show_cmd "$P"
  done
fi

hr "2. 运行环境与版本"
if [ -n "$PID" ]; then
  EXE=$(readlink -f "/proc/$PID/exe" 2>/dev/null); echo "  exe   : $EXE"
  echo "  cwd   : $(readlink -f /proc/$PID/cwd 2>/dev/null)"
  echo "  cgroup: $(head -1 /proc/$PID/cgroup 2>/dev/null | cut -c1-120)"
  if [ -n "$EXE" ] && [ -x "$EXE" ]; then
    "$EXE" -c "import sys;print('  python:',sys.version.split()[0])" 2>/dev/null
    "$EXE" -c "import vllm;print('  vllm  :',vllm.__version__)" 2>/dev/null
    "$EXE" -c "import sglang;print('  sglang:',sglang.__version__)" 2>/dev/null
    "$EXE" -m pip show vllm 2>/dev/null | head -2 | sed 's/^/  /'
  fi
fi

hr "3. 探活 + 模型名/上下文"
if [ -n "$KEY" ]; then
  curl -s -m 10 -H "Authorization: Bearer $KEY" "$BASE/v1/models" | head -c 600; echo
else
  echo "  （没给 KEY，跳过；可加 KEY=xxx 重跑）"
fi

hr "4. 运行时指标（并发 / KV cache / 前缀缓存命中 / 成功失败数）"
if [ -n "$KEY" ]; then
  curl -s -m 10 -H "Authorization: Bearer $KEY" "$BASE/metrics" \
    | grep -E "^vllm:(num_requests_running|num_requests_waiting|num_requests_swapped|gpu_cache_usage_perc|cpu_cache_usage_perc|prefix_cache_hits_total|prefix_cache_queries_total|prompt_tokens_total|generation_tokens_total|request_success_total|request_failure_total)" \
    | grep -v "^#" || echo "  （拿不到 /metrics）"
else
  echo "  （没给 KEY，跳过）"
fi

hr "5. GPU ↔ 进程对应（确认哪 4 张卡是 Hy3）"
nvidia-smi 2>/dev/null | sed -n '/Processes/,$p' | head -20

hr "完成 —— 把 0/1/2 节整段贴回即可（已打码）"
