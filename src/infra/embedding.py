# -*- coding: utf-8 -*-
"""Embedding 模型封装 —— 懒加载单例，提供文本向量化接口

模型：BAAI/bge-small-zh-v1.5（512维，~100MB，中文优化）
首次调用自动下载到 ~/.cache/huggingface/。
国内网络慢可设 HF_ENDPOINT=https://hf-mirror.com。
"""

import sys
import os
from typing import Optional

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_model = None


def get_embedding_model():
    """懒加载单例——优先用本地缓存（离线模式），模型已缓存时不请求远程"""
    global _model
    if _model is None:
        import os as _os
        # 离线模式：模型已缓存就直接用本地，不请求 huggingface.co（避免被墙超时）
        # 注意：换新模型/删缓存后需临时去掉这行，否则离线找不到模型会失败
        _os.environ["HF_HUB_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        print(f"  ⏳ 加载 embedding 模型: {_EMBEDDING_MODEL_NAME} ...")
        _model = SentenceTransformer(_EMBEDDING_MODEL_NAME)
        print(f"  ✅ 模型就绪 ({_model.get_embedding_dimension()} 维)")
    return _model


def embed_text(text: str) -> list[float]:
    """单文本向量化"""
    model = get_embedding_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """批量文本向量化"""
    model = get_embedding_model()
    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    return embeddings.tolist()


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("Embedding 模型自测")
    print("=" * 50)

    model = get_embedding_model()
    dim = model.get_embedding_dimension()
    print(f"  维度: {dim}")

    texts = [
        "橘猫在窗台上晒太阳，眯着眼睛打盹",
        "美短追着激光笔的光点满屋子跑",
        "两只猫在西湖边散步，夕阳把毛发染成金色",
    ]
    vecs = embed_texts(texts)
    print(f"  编码 {len(texts)} 条文本 → 每条 {len(vecs[0])} 维")
    print(f"  示例向量前 5 个值: {[round(v, 4) for v in vecs[0][:5]]}")
    print("  ✅ Embedding 模型正常")
