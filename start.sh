#!/usr/bin/env bash
# 智小税 · 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ ! -f data/companies.json ]; then
  echo "首次启动：自动创建空白账套；如需演示数据可运行 .venv/bin/python scripts/seed_demo.py --reset"
fi
exec .venv/bin/streamlit run ui/streamlit_app.py
