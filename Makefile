# 税小智 · 常用命令（开发/部署辅助）
PY ?= .venv/bin/python
STREAMLIT ?= .venv/bin/streamlit

.PHONY: help setup test run demo docker docker-run backup clean

help:  ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup:  ## 创建虚拟环境并安装依赖
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:  ## 运行全量离线测试
	$(PY) -m pytest tests/ -q

run:  ## 启动 Streamlit
	$(STREAMLIT) run ui/streamlit_app.py

demo:  ## 生成演示账套（虚构数据）
	$(PY) scripts/seed_demo.py --reset

docker:  ## 构建 Docker 镜像
	docker build -t tax-agent .

docker-run:  ## 运行容器（挂载 data）
	docker run -d --name tax-agent -p 8501:8501 -v "$$(pwd)/data:/app/data" tax-agent

backup:  ## 备份当前公司账套
	$(PY) main.py backup --tag makefile

clean:  ## 清理缓存（不删除数据）
	rm -rf __pycache__ **/__pycache__ .pytest_cache
