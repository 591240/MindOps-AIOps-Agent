# MindOps 后端运行镜像。
# 依赖全部有 cp313 manylinux wheel（cryptography / pymilvus / tiktoken），
# 因此不装 build-essential，镜像能压在 ~450MB。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖再拷代码：改业务代码不会重新解析依赖层。
# 用 requirements.lock（完整解析结果）而不是 requirements.txt（顶层约束），
# 保证生产镜像与验证过的版本一致。
COPY requirements.lock ./
# 国内直连 pypi.org 常在几百 KB 处停住；把索引做成构建参数，公开仓库保留默认值，
# 本机开发在 docker-compose.dev.yml 里覆盖成镜像源。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.lock

COPY app/ ./app/
COPY config/ ./config/

# 运行期要写的目录：embedding 缓存与 Milvus Lite 文件（若配置成本地路径）
RUN mkdir -p /app/data \
    && groupadd -r mindops && useradd -r -g mindops mindops \
    && chown -R mindops:mindops /app
USER mindops

EXPOSE 8010

# 镜像里没有 curl，用标准库探活
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/health', timeout=3).status == 200 else 1)"

# --workers 1 是有意的：事件锁（app/incidents/cluster.py）与诊断并发信号量是
# 进程内对象，多 worker 会对同一事件重复起诊断。配了 Redis 只解决去重/限流/SSE 广播，
# 不解决这两处。要横向扩容得先把锁改成 Redis 实现。
#
# --no-access-log：SSE 短票只能走 query，uvicorn 默认访问日志会把整条 URL
# （含 token）写进容器日志。访问日志统一由前置 Nginx 记录（只记 $uri，不含 query）。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--no-access-log"]
