FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

# 容器内需监听所有接口，由平台网关/反向代理负责对外访问控制
ENV HOST=0.0.0.0 PORT=8787
EXPOSE 8787

# 以非 root 运行
RUN useradd -m -u 10001 appuser
USER appuser

CMD ["python3", "app.py"]
