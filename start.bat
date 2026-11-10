@echo off
REM 智小税 · 一键启动（Windows）
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\streamlit run ui\streamlit_app.py
pause
