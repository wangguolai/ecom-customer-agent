# -*- coding: utf-8 -*-
# 电商客服后端镜像 —— FastAPI + uvicorn（模块 7 部署运维）
#
# 只打包后端（src/backend），agent/RAG 链路（sentence-transformers + Qdrant 本地文件模式）
# 留在宿主机跑——Qdrant 本地文件模式不是服务，容器化要换 Qdrant server，属于「后续扩展」不在本模块范围。

FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE=1：不生成 .pyc（镜像更干净）
# PYTHONUNBUFFERED=1：stdout 不缓冲，日志实时出（否则被 Docker 缓冲吞掉，排障看不到）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先复制依赖清单再装：利用 Docker 层缓存，requirements 不变 → pip 这层不重跑，构建秒级
COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt

# 后复制源码：源码常改，放后面，改动只重建 COPY 这一层
COPY src/ src/

# 降权到非 root（Prompt Injection 架构级防御 —— 沙箱隔离层）
# 位置有讲究：必须放在 pip install + COPY 之后。放前面会因为没有写权限，
# 装依赖和拷代码直接失败。
# 为什么要降权：容器默认以 root 跑，一旦注入链路最终导致容器内命令执行，
# 攻击者拿到的就是 root；降权后拿到的是无特权账号，配合 compose 的 cap_drop，
# 横向移动到宿主机的成本大幅升高。
# 本服务运行时无本地写需求（PYTHONDONTWRITEBYTECODE=1 禁 .pyc、无文件日志、
# 数据全在 MySQL/Redis），所以降权不会踩到写权限问题。
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --host 0.0.0.0：容器内 127.0.0.1 只在容器内部可达，不监听 0.0.0.0 宿主机访问不到
CMD ["uvicorn", "src.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
