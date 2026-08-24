# -*- coding: utf-8 -*-
"""MySQL 连接池封装 —— DBUtils PooledDB + python-dotenv 读配置

为什么连接池：每请求「TCP 握手 + MySQL 认证」新建连接贵，池化复用。
参数：
  maxconnections=5   池上限（demo 够）
  mincached=1        预热一个连接
  maxcached=5        闲置连接上限
  ping=1             每次取连接前 ping，检测被 MySQL wait_timeout 杀掉的死连接
  autocommit=True    读接口无事务需求；写接口（refund）显式 conn.begin() 开事务
"""

import os
import sys

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
import pymysql
from dbutils.pooled_db import PooledDB

load_dotenv(os.path.join(_project_root, ".env"))

# 连接池（进程级单例：进程启动时建，进程退出靠 OS 回收；多 worker 各自持池，--reload 会重建）
_pool = PooledDB(
    creator=pymysql,
    maxconnections=5,
    mincached=1,
    maxcached=5,
    ping=1,
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    database=os.environ.get("MYSQL_DB", "ecommerce"),
    charset="utf8mb4",
    autocommit=True,
)


def get_conn():
    """从连接池取一个连接。用完必须 close_conn() 归还，否则连接泄漏、池耗尽后续请求卡死。"""
    return _pool.connection()


def close_conn(conn):
    """归还连接到池（放回池复用，不是真正关闭）。"""
    if conn is not None:
        conn.close()
