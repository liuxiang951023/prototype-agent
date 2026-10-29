#!/usr/bin/env bash
# 启动 Docx → 原型 AI Agent 服务
set -e

cd "$(dirname "$0")"

# 1. 虚拟环境
if [ ! -d .venv ]; then
  echo "[1/3] 创建虚拟环境..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# 2. 依赖
if [ ! -f .venv/.deps_installed ]; then
  echo "[2/3] 安装依赖..."
  pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple
  touch .venv/.deps_installed
else
  echo "[2/3] 依赖已安装，跳过"
fi

# 3. 环境变量
if [ ! -f .env ]; then
  echo "⚠️  未发现 .env 文件，请先复制 .env.example 并填入 LLM_API_KEY"
  echo "   cp .env.example .env"
  echo "   vim .env"
  exit 1
fi
# shellcheck disable=SC1090
set -a; source .env; set +a

PORT="${APP_PORT:-8000}"
echo "[3/3] 启动服务：http://localhost:${PORT}"
# 不带 --reload，避免长任务跑 Agent 时被文件变化干扰导致 worker 中断
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
