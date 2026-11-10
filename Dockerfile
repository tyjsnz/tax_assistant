# 税小智 · 小规模纳税人自主税务填报系统
# 构建：docker build -t tax-agent .
# 运行：docker run -p 8501:8501 -v $(pwd)/data:/app/data tax-agent
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# 运行依赖（OCR 的 macOS Vision 仅本机可用；容器内可导入 XML/OFD、或手动粘贴文本）
COPY requirements.txt ./
# 国内服务器可用镜像源构建：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY . .

EXPOSE 8501

# 数据目录挂载到 /app/data 可持久化账套；首次启动自动初始化默认公司并迁移旧数据
VOLUME ["/app/data"]

CMD ["streamlit", "run", "ui/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
