# -*- coding: utf-8 -*-
"""模型预热 —— 主线程预加载 embedding + rerank，根治 Windows CUDA 死锁

torch 模型（embedding / rerank）是懒加载单例，首次加载若发生在 asyncio.to_thread 的
worker 线程里，Windows 下 CUDA 初始化会死锁（worker 线程 idle、Future 永不 resolve，
表现为「挂起」——不返回也不报错，而非「报错」）。

为什么首次加载会落进 worker 线程：检索工具（search_products / get_return_policy）为了不阻塞
事件循环，把 embedding + rerank 的 CPU/GPU 密集计算丢进 to_thread。首次调用时模型还没加载，
于是「加载」和「推理」一起落进 worker 线程 → torch 在 worker 线程初始化 CUDA → 死锁。

修复：进入 asyncio 事件循环之前，在主线程同步预热两个模型。之后检索在 to_thread 里只是
「用」已加载的单例（不再触发加载），死锁不再发生。幂等：模型已加载则直接返回。
"""

import sys
import os

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def warmup_models():
    """主线程预热 embedding + rerank。

    必须在 asyncio.run(...) 之前调用（否则已进事件循环，to_thread 可能先触发懒加载）。
    幂等：模型是单例，已加载则直接返回；rerank 加载失败会打印降级警告（内部已兜底，不抛异常）。

    ⚠️ **不能只「加载模型」，还要跑一次真实推理**：
    `get_embedding_model()` 只是把权重读进内存；**首次 encode 本身另有开销**
    （tokenizer 首次调用、CUDA context/kernel 首次建立、显存缓存分配）。
    实测（2026-09-18）：首次 `embed_text` **213ms**，之后 **3ms**。
    只加载不推理的话，这 200ms+ 会落到**进程内第一个请求**头上——
    表现为「服务刚起来时第一句话特别慢」（实测首个 delta 3983ms vs 稳定态 1990ms，
    差的这 2 秒里就有它的份）。预热一下 = 把首次开销前移到启动期，用户无感。
    """
    from src.infra.embedding import get_embedding_model, embed_text
    from src.infra.reranker import get_reranker, rerank

    get_embedding_model()
    embed_text("预热")          # 真实 encode 一次（把首次 tokenizer/CUDA 开销前移）

    if get_reranker() is not None:
        try:
            # 同样跑一次真实推理。rerank 内部已对不可用做降级，这里再包一层是防
            # 「加载成功但 predict 抛异常」把启动搞挂——预热失败不该挡住服务启动。
            rerank("预热", ["预热文本"])
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ rerank 预热推理失败（不影响启动，首次真实调用时会重试）："
                  f"{type(e).__name__}", file=sys.stderr)
