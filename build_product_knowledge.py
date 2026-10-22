"""增量向量化商品；全部就绪才发布为活动知识库，可安全重跑和恢复。"""
from __future__ import annotations

import argparse
import json
import os

from embedding_client import EmbeddingError, get_embedding_client, vector_literal
from postgres_config import PROJECT_DIR, connect_database, load_project_env
from product_knowledge import source_documents


def build_knowledge(*, dry_run: bool = False) -> dict:
    documents = source_documents()
    if not documents:
        raise RuntimeError("没有可向量化的商品描述，请先导入 AdventureWorks 数据")
    if dry_run:
        return {"mode": "dry_run", "eligible_products": len(documents),
                "characters": sum(len(d["content"]) for d in documents),
                "note": "只统计文档，未调用 Embedding API，也未写入向量。"}
    client = get_embedding_client()
    settings = client.settings
    load_project_env()
    batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
    if not 1 <= batch_size <= 10:
        raise EmbeddingError("EMBEDDING_BATCH_SIZE 应为 1–10")
    from psycopg.types.json import Jsonb

    with connect_database(importing=True) as conn:
        conn.execute((PROJECT_DIR / "schema_product_knowledge.sql").read_text())
        conn.execute("INSERT INTO knowledge_profiles(profile_id,configuration) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                     (settings.profile_id, Jsonb(settings.public_configuration())))
        old = conn.execute("SELECT product_id,content_hash FROM product_knowledge WHERE profile_id=%s",
                           (settings.profile_id,)).fetchall()
    old_hashes = {r["product_id"]: r["content_hash"] for r in old}
    pending = [d for d in documents if old_hashes.get(d["product_id"]) != d["content_hash"]]
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset: offset + batch_size]
        vectors = client.embed_documents([d["content"] for d in batch])
        with connect_database() as conn:
            for document, vector in zip(batch, vectors):
                conn.execute(
                    """INSERT INTO product_knowledge(profile_id,product_id,content,content_hash,embedding_dimensions,embedding)
                       VALUES (%s,%s,%s,%s,%s,%s::public.vector)
                       ON CONFLICT (profile_id,product_id) DO UPDATE SET
                         content=EXCLUDED.content,content_hash=EXCLUDED.content_hash,
                         embedding_dimensions=EXCLUDED.embedding_dimensions,embedding=EXCLUDED.embedding,
                         updated_at=CURRENT_TIMESTAMP""",
                    (settings.profile_id, document["product_id"], document["content"], document["content_hash"],
                     settings.dimensions, vector_literal(vector)),
                )
        print(f"[向量化] {min(offset + batch_size, len(pending))}/{len(pending)}", flush=True)
    # 防止建库期间源描述变化后，错误地把旧快照发布为当前知识库。
    if source_documents() != documents:
        raise RuntimeError("建库期间商品描述发生变化，未切换活动知识库；请重新运行")
    with connect_database() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(48151332)")
        stored = conn.execute("SELECT product_id,content_hash FROM product_knowledge WHERE profile_id=%s",
                              (settings.profile_id,)).fetchall()
        stored_hashes = {r["product_id"]: r["content_hash"] for r in stored}
        if any(stored_hashes.get(d["product_id"]) != d["content_hash"] for d in documents):
            raise RuntimeError("商品向量不完整，未发布活动知识库")
        conn.execute("DELETE FROM product_knowledge WHERE profile_id=%s AND NOT(product_id=ANY(%s))",
                     (settings.profile_id, [d["product_id"] for d in documents]))
        conn.execute(
            """INSERT INTO knowledge_settings(singleton,active_profile_id) VALUES (true,%s)
               ON CONFLICT (singleton) DO UPDATE SET active_profile_id=EXCLUDED.active_profile_id,
               activated_at=CURRENT_TIMESTAMP""", (settings.profile_id,),
        )
    return {"mode": "ready", "products": len(documents), "new_or_updated": len(pending),
            "reused": len(documents) - len(pending),
            "model": settings.public_configuration()["model"],
            "dimensions": settings.dimensions, "profile_id": settings.profile_id}


def main():
    parser = argparse.ArgumentParser(description="商品描述向量化及 pgvector 入库")
    parser.add_argument("--dry-run", action="store_true", help="只统计文档，不调用 API")
    args = parser.parse_args()
    print(json.dumps(build_knowledge(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
