#!/usr/bin/env bash
# 找出这台机器上"真正在提供模型推理的那个服务"在哪。
#
# 背景：127.0.0.1:8000 是普通 uvicorn（/v1/models 返回 404），不是 OpenAI 兼容的模型服务；
#      但 8 张 A800 里有 7 张各占 ~74-75GB，说明大模型确实在跑，只是端口/进程还没找到。
#
# 用法：  bash find_llm_service.sh
# 只读，不改任何东西。

set -u
hr() { echo; echo "======== $* ========"; }
mask() { sed -E 's/((api[-_]?key|token|secret)["'"'"'=: ]+)[^ "'"'"']+/\1***MASKED***/g'; }

hr "A. GPU 上那几个进程到底是什么（⭐ 关键：命令行里有端口和 --served-model-name）"
for PID in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
  echo "--- pid $PID ---"
  ps -p "$PID" -o pid,ppid,etime,args --no-headers 2>/dev/null | cut -c1-500 | mask
done

hr "B. 全部监听端口（重点找 8000 以外 + 那个 pid 对应的端口）"
if command -v ss >/dev/null 2>&1; then
  ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | sed 's/  */ /g' | cut -c1-160
else
  netstat -ltnp 2>/dev/null | grep LISTEN | cut -c1-160
fi

hr "C. 8000 上的 uvicorn 是谁（它大概是你们自己的应用，不是模型）"
ps -p 2227225 -o pid,etime,args --no-headers 2>/dev/null | cut -c1-300 | mask
echo -n "   / 根路径: "; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
echo -n "   /docs   : "; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/docs
curl -s -m 5 http://127.0.0.1:8000/ | head -c 200; echo

hr "D. 全部容器（含未运行的；找 vllm / xinference / ollama 之类）"
docker ps -a --format '{{.Names}} | {{.Image}} | {{.Ports}} | {{.Status}}' 2>/dev/null | head -30

hr "E. ⭐ open-webui 指向哪个模型端点（它必须配一个 OpenAI 兼容地址，答案大概率在这）"
docker exec open-webui sh -c 'env | grep -iE "openai|api_base|base_url|ollama|vendor|model" ' 2>/dev/null | mask | head -20
echo "--- 它的配置文件里也翻一下 ---"
docker exec open-webui sh -c 'cat /app/backend/data/config.json 2>/dev/null | head -c 800' 2>/dev/null
echo

hr "F. 试一圈常见端口（谁返回 200 就是它）"
for PORT in 8001 8002 8080 8081 9000 9997 11434 23333 30000 5000 7860 1234; do
  CODE=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null)
  if [ "$CODE" = "200" ]; then
    echo ">>> http://127.0.0.1:${PORT}/v1/models  == 200  ⭐ 找到模型服务了"
    curl -s -m 5 "http://127.0.0.1:${PORT}/v1/models" | head -c 400; echo
  else
    echo "    port ${PORT}: ${CODE}"
  fi
done

hr "G. 有没有通过 unix socket / 只绑内网的其它服务"
ls -l /tmp/*.sock /var/run/*.sock 2>/dev/null | head
ss -lx 2>/dev/null | head -10

hr "完成 —— 把 A~G 的输出整段贴回即可"
