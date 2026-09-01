# -*- coding: utf-8 -*-
"""知识库索引——读 products.md + policies.md 分块，向量化后写入 Qdrant（商品 + 政策两个知识域）

两种模式：
- build_knowledge_base()：全量重建（初始化 / 数据大改时用）
- update_knowledge_base()：增量更新（日常增删改商品/政策时用，只处理差异）

chunk_id 用稳定标识（不是 index）：
- 商品：products:{product_id}（P001 等稳定实体 ID，三处对齐的桥）
- 政策：policies:{title}（政策标题）
稳定 chunk_id 是增量对比的锚点——中间删一个商品，后面商品的 chunk_id 不会错位，
_index 编号会（products:3 变 products:2），所以不能用 index 做增量锚点。
"""

import sys
import os

# 把项目根目录加进 sys.path，让 from src.infra.xxx import 能找到
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _build_payloads():
    """解析源数据 → 生成 payloads 列表（全量/增量共用，保证两种模式 chunk_id 规则一致）"""
    from src.domain.products import parse_products
    from src.domain.policies import parse_policies

    products = parse_products()
    policies = parse_policies()
    payloads = []

    for i, p in enumerate(products):
        payloads.append({
            "chunk_id": f"products:{p.id}",
            "text": p.raw_chunk,
            "title": p.title,
            "category": p.category,
            "source_file": "products.md",
            "chunk_index": i,
            "product_id": p.id,  # 稳定实体 ID，打通「向量检索 → MySQL 查价」链路
            "kb_type": "product",
        })
    for i, p in enumerate(policies):
        payloads.append({
            "chunk_id": f"policies:{p.title}",
            "text": p.raw_chunk,
            "title": p.title,
            "category": "",
            "source_file": "policies.md",
            "chunk_index": i,
            "product_id": "",
            "kb_type": "policy",
        })
    return products, policies, payloads


def build_knowledge_base():
    """全量重建：删掉两个源的全部旧 chunk，再全量 embedding + upsert（幂等）。

    适合：初始化、schema 变更、大批量改数据。成本 = 重算全部 embedding（几十商品几秒，够用）。
    """
    from src.infra.embedding import embed_texts
    from src.infra.vector_store import QdrantStore

    products, policies, payloads = _build_payloads()
    print(f"📦 商品分块：{len(products)} 块，政策分块：{len(policies)} 块")

    # 向量化（同构：BGE-small-zh 512 维 COSINE，逻辑多库共享 collection）
    vectors = embed_texts([p["text"] for p in payloads])

    store = QdrantStore()
    store.ensure_collections()
    store.delete_by_source("product_knowledge", "products.md")
    store.delete_by_source("product_knowledge", "policies.md")
    store.upsert_knowledge(payloads, vectors)

    info = store.collection_info("product_knowledge")
    print(f"✅ 全量重建完成：product_knowledge {info['points_count']} points（商品 {len(products)} + 政策 {len(policies)}）")
    store.close()  # 显式释放文件锁，允许同进程后续再开新实例（不靠进程退出 atexit 兜底）


def update_knowledge_base():
    """增量更新：对比「源数据当前状态」vs「向量库现状」，只处理差异。

    - 新增：源有、库无 → embedding + upsert
    - 修改：源库都有、text 变了 → embedding + upsert（_point_id 幂等覆盖，不产生重复）
    - 删除（下架）：库有、源无 → 按 chunk_id 删（delete_by_chunk_ids，只删那一条）
    - 不变：跳过（不重新 embedding，省模型推理成本）

    核心价值：省掉「没变化的 chunk 的 embedding 重算」——embedding 是贵的（BGE 模型推理），
    scroll + BM25 建索引是便宜的（不用模型）。数据量大时这个差距才显出来。

    注意：这是「离线更新」语义——更新后需重启服务，进程内的 BM25 索引才会从 Qdrant
    scroll 重建（demo 规模下商品更新是离线操作，非在线实时生效）。
    """
    from src.infra.embedding import embed_texts
    from src.infra.vector_store import QdrantStore

    _, _, payloads = _build_payloads()
    desired = {p["chunk_id"]: p for p in payloads}

    store = QdrantStore()
    store.ensure_collections()

    # 库内现状：{chunk_id: text}（scroll_all 返回 (chunk_id, text, title, category, product_id, kb_type)）
    existing = {cid: text for cid, text, _, _, _, _ in store.scroll_all()}

    to_upsert = []   # 新增 + 修改
    to_delete = []   # 删除（下架）
    unchanged = 0
    for cid, payload in desired.items():
        if cid not in existing:
            to_upsert.append(payload)          # 新增
        elif existing[cid] != payload["text"]:
            to_upsert.append(payload)          # 修改（text 变了）
        else:
            unchanged += 1                     # 不变
    for cid in existing:
        if cid not in desired:
            to_delete.append(cid)              # 删除（源里没了 = 下架）

    if not to_upsert and not to_delete:
        print("✅ 增量更新：无变化，跳过。")
        store.close()  # 显式释放文件锁，允许同进程后续再开新实例（不靠进程退出 atexit 兜底）
        return

    if to_upsert:
        vectors = embed_texts([p["text"] for p in to_upsert])
        store.upsert_knowledge(to_upsert, vectors)
    if to_delete:
        store.delete_by_chunk_ids("product_knowledge", to_delete)

    info = store.collection_info("product_knowledge")
    print(f"✅ 增量更新完成：新增/修改 {len(to_upsert)}，删除 {len(to_delete)}，不变 {unchanged}。"
          f"库内共 {info['points_count']} points。")
    store.close()  # 显式释放文件锁，允许同进程后续再开新实例（不靠进程退出 atexit 兜底）


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="知识库索引（全量重建 / 增量更新）")
    parser.add_argument("--update", action="store_true", help="增量更新（默认全量重建）")
    args = parser.parse_args()
    if args.update:
        update_knowledge_base()
    else:
        build_knowledge_base()
