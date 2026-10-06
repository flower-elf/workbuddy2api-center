# WorkBuddy Multi-Account Reverse Proxy Gateway
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1     HOST=0.0.0.0     PORT=8788     API_KEY=     TZ=Asia/Shanghai

WORKDIR /app

RUN apk add --no-cache tzdata ca-certificates &&     cp /usr/share/zoneinfo/${TZ} /etc/localtime &&     echo "${TZ}" > /etc/timezone

# Copy app files; no pip dependencies needed
COPY app/ ./app/

RUN mkdir -p /app/accounts /app/usage

# Persist credentials and usage logs
VOLUME ["/app/accounts", "/app/usage"]

EXPOSE 8788

# 端口探活：账号池为空时 /health 会返回 503，因此健康检查只确认端口已监听
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('PORT', '8788'))), 3).close()"

# --lan 监听所有网卡并强制 API Key，首次生成后写入 ./accounts/settings.json 并打印在日志。
# 不写 --port：网关自己读 PORT；exec 形式让 python 保持 PID 1，docker stop 才能送达 SIGTERM。
CMD ["python", "app/wb_proxy.py", "--host", "0.0.0.0", "--lan"]
