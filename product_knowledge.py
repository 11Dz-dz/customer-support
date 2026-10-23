from __future__ import annotations

from decimal import Decimal
import hashlib
import logging
import os

from embedding_client import EmbeddingError, get_embedding_client, vector_literal
from postgres_config import connect_database


logger = logging.getLogger(__name__)


def product_document(product: dict) -> str | None:
    """一件商品一份短文档；没有描述的商品不补造知识。"""
    if not product.get("description_en") and not product.get("description_zh"):
        return None
    fields = [
        ("商品", product["name"]), ("型号", product["product_number"]),
        ("颜色", product.get("color")), ("尺寸", product.get("size")),
        ("重量", f"{product['weight']} {product.get('weight_unit') or ''}" if product.get("weight") else None),
        ("中文描述", product.get("description_zh")), ("English description", product.get("description_en")),
    ]
    # 价格、库存和订单状态不写入语义向量，召回后从业务表读取。
    return "\n".join(f"{name}: {value}" for name, value in fields if value is not None and str(value).strip())


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def source_documents(conn=None) -> list[dict]:
    if conn is None:
        with connect_database() as connection:
            return source_documents(conn=connection)
    catalog = conn.execute("SELECT * FROM product_catalog ORDER BY product_id").fetchall()
    return [{"product_id": p["product_id"], "content": text, "content_hash": content_hash(text)}
            for p in catalog if (text := product_document(p)) is not None]


def reciprocal_rank_fusion(keyword_rows: list[dict], semantic_rows: list[dict], limit: int):
    scores, products, signals = {}, {}, {}
    for source, rows in (("keyword", keyword_rows), ("semantic", semantic_rows)):
        for rank, row in enumerate(rows, 1):
            product_id = row["product_id"]
            products.setdefault(product_id, {}).update(row)
            scores[product_id] = scores.get(product_id, 0.0) + 1.0 / (60 + rank)
            signals.setdefault(product_id, []).append(source)
    ranked = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    return [{**products[k], "retrieval_sources": signals[k], "retrieval_score": round(scores[k], 8)} for k in ranked]


def hybrid_retrieve(query: str, keyword_rows: list[dict], limit: int, *, min_price=None, max_price=None, product_category=None) -> dict:
    from adventureworks_service import price_conditions
    clauses, price_parameters = price_conditions(min_price, max_price, alias="p.")
    from catalog_filters import category_conditions, category_matches
    category_clauses,category_parameters=category_conditions(product_category,alias='p.')
    clauses+=category_clauses
    price_parameters+=category_parameters
    keyword_rows=[p for p in keyword_rows if category_matches(p,product_category)]
    client = get_embedding_client()
    profile_id = client.settings.profile_id
    # 先检查完整建库标记，再调用 API。没有就绪知识库时不产生不必要的 API 请求。
    with connect_database() as conn:
        exists = conn.execute("SELECT to_regclass('customer_service.knowledge_settings') AS name").fetchone()
        if not exists["name"]:
            raise EmbeddingError("商品向量表尚未初始化")
        active = conn.execute("""SELECT s.active_profile_id,s.active_build_id FROM knowledge_settings s
            JOIN knowledge_builds b ON b.build_id=s.active_build_id AND b.profile_id=s.active_profile_id
            WHERE s.singleton=true AND b.status='ready'""").fetchone()
        if not active:
            raise EmbeddingError("商品向量尚未完成入库")
        if active["active_profile_id"] != profile_id:
            raise EmbeddingError("Embedding 配置与已入库模型不一致，请重新建库后再检索")
    vector = vector_literal(client.embed_query(query))
    try:
        threshold = float(os.getenv("SEMANTIC_MIN_SIMILARITY", "0.35"))
    except ValueError:
        raise EmbeddingError("SEMANTIC_MIN_SIMILARITY 必须是数值") from None
    if not -1 <= threshold <= 1:
        raise EmbeddingError("SEMANTIC_MIN_SIMILARITY 必须在 -1 到 1 之间")
    with connect_database() as conn:
        semantic_rows = conn.execute(
            f"""SELECT p.product_id,p.name,p.product_number,p.product_subcategory_id,p.color,p.size,p.weight,p.weight_unit,
                      p.list_price,p.description_en,p.description_zh,p.sell_end_at,p.discontinued_at,
                      k.content AS retrieved_content, 1-(k.embedding <=> %s::public.vector) AS similarity
               FROM knowledge_build_products k JOIN product_catalog p ON p.product_id=k.product_id
               WHERE k.build_id=%s AND k.embedding_dimensions=%s
                 AND {' AND '.join(clauses)}
                 AND 1-(k.embedding <=> %s::public.vector) >= %s
               ORDER BY k.embedding <=> %s::public.vector, p.product_id LIMIT %s""",
            [vector, active['active_build_id'], client.settings.dimensions, *price_parameters,
             vector, threshold, vector, max(20, limit)],
        ).fetchall()
    # 不返回已变化描述对应的旧向量；商品更改后需要重新运行入库脚本。
    fresh_rows = [r for r in semantic_rows if product_document(r) == r["retrieved_content"]]
    logger.info("product_retrieval mode=hybrid keyword_candidates=%d semantic_candidates=%d",
                len(keyword_rows), len(fresh_rows))
    return {
        "products": reciprocal_rank_fusion(keyword_rows, fresh_rows, limit),
        "retrieval_mode": "hybrid", "semantic_available": True,
        "embedding_model": client.settings.public_configuration()["model"],
        "note": "关键词与向量融合召回；相似度不是适用性证明。价格等业务字段查询时从商品表读取。",
    }
