"""增量准备独立构建；完整验证后原子发布，失败批次永不覆盖活动向量。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from uuid import uuid4

from embedding_client import EmbeddingError, get_embedding_client, vector_literal
from postgres_config import PROJECT_DIR, connect_database, load_project_env
from product_knowledge import source_documents

BUILD_LOCK = 48151332


def _fingerprint(documents):
    return hashlib.sha256(json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initialize_knowledge_schema():
    """幂等迁移现有活动向量，不重新计算Embedding。"""
    with connect_database(importing=True) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (BUILD_LOCK,))
        conn.execute((PROJECT_DIR/'schema_product_knowledge.sql').read_text())


def _start_build(build_id, settings, documents):
    with connect_database() as conn:
        conn.execute('INSERT INTO knowledge_profiles(profile_id,configuration) VALUES (%s,%s::jsonb) ON CONFLICT DO NOTHING',
                     (settings.profile_id,json.dumps(settings.public_configuration())))
        conn.execute("""INSERT INTO knowledge_builds(build_id,profile_id,status,source_hash,expected_count)
                        VALUES (%s,%s,'building',%s,%s)""",
                     (build_id,settings.profile_id,_fingerprint(documents),len(documents)))
        # 只复用同配置、相同文本与哈希的向量；失败构建可供续建复用。
        old=conn.execute("""SELECT k.build_id,k.product_id,k.content,k.content_hash,k.embedding_dimensions
            FROM knowledge_build_products k JOIN knowledge_builds b USING(build_id)
            WHERE b.profile_id=%s ORDER BY b.created_at DESC,k.build_id""",(settings.profile_id,)).fetchall()
        candidates={}
        for row in old:
            candidates.setdefault((row['product_id'],row['content_hash'],row['content'],row['embedding_dimensions']),row['build_id'])
        pending=[]
        for document in documents:
            key=(document['product_id'],document['content_hash'],document['content'],settings.dimensions)
            previous=candidates.get(key)
            if previous is None:
                pending.append(document)
                continue
            cursor=conn.execute("""INSERT INTO knowledge_build_products
                (build_id,product_id,content,content_hash,embedding_dimensions,embedding)
                SELECT %s,product_id,content,content_hash,embedding_dimensions,embedding
                FROM knowledge_build_products WHERE build_id=%s AND product_id=%s
                    AND content_hash=%s AND content=%s AND embedding_dimensions=%s""",
                (build_id,previous,*key))
            if cursor.rowcount!=1:
                raise RuntimeError('待复用向量发生变化，构建未发布')
    return pending


def _write_batch(build_id,settings,batch,vectors):
    if len(vectors)!=len(batch):
        raise EmbeddingError('Embedding返回数量与文档数量不一致')
    with connect_database() as conn:
        for document,vector in zip(batch,vectors):
            # 不覆盖已有记录；重复或错误写入使当前批次事务回滚。
            conn.execute("""INSERT INTO knowledge_build_products
                (build_id,product_id,content,content_hash,embedding_dimensions,embedding)
                VALUES (%s,%s,%s,%s,%s,%s::public.vector)""",
                (build_id,document['product_id'],document['content'],document['content_hash'],
                 settings.dimensions,vector_literal(vector)))


def _publish_build(build_id,settings,documents):
    with connect_database() as conn:
        # 源检查和发布处于同一事务，短暂锁住源表，关闭检查后的变更窗口。
        conn.execute('LOCK TABLE products,product_descriptions,product_model_descriptions IN SHARE MODE')
        if source_documents(conn=conn)!=documents:
            raise RuntimeError('建库期间商品描述发生变化，构建未发布；请重新运行')
        stored=conn.execute("""SELECT product_id,content,content_hash,embedding_dimensions
            FROM knowledge_build_products WHERE build_id=%s""",(build_id,)).fetchall()
        expected={d['product_id']:(d['content'],d['content_hash'],settings.dimensions) for d in documents}
        actual={d['product_id']:(d['content'],d['content_hash'],d['embedding_dimensions']) for d in stored}
        if actual!=expected or len(stored)!=len(documents):
            raise RuntimeError('商品向量不完整或与源快照不一致，构建未发布')
        cursor=conn.execute("""UPDATE knowledge_builds SET status='ready',finished_at=CURRENT_TIMESTAMP
            WHERE build_id=%s AND profile_id=%s AND status='building' AND source_hash=%s AND expected_count=%s""",
            (build_id,settings.profile_id,_fingerprint(documents),len(documents)))
        if cursor.rowcount!=1:
            raise RuntimeError('构建元信息不一致，构建未发布')
        conn.execute("""INSERT INTO knowledge_settings(singleton,active_profile_id,active_build_id)
            VALUES (true,%s,%s) ON CONFLICT(singleton) DO UPDATE
            SET active_profile_id=EXCLUDED.active_profile_id,active_build_id=EXCLUDED.active_build_id,
                activated_at=CURRENT_TIMESTAMP""",(settings.profile_id,build_id))


def build_knowledge(*,dry_run: bool=False)->dict:
    if dry_run:
        documents=source_documents()
        if not documents:
            raise RuntimeError('没有可向量化的商品描述，请先导入AdventureWorks数据')
        return {'mode':'dry_run','eligible_products':len(documents),
                'characters':sum(len(d['content']) for d in documents),
                'note':'只统计文档，未调用Embedding，也未写入向量。'}
    load_project_env()
    batch_size=int(os.getenv('EMBEDDING_BATCH_SIZE','10'))
    if not 1<=batch_size<=10:
        raise EmbeddingError('EMBEDDING_BATCH_SIZE应为1–10')
    client=get_embedding_client()
    settings=client.settings
    build_id=uuid4().hex
    # 锁覆盖整个构建；连接保持，但不在Embedding期间保持长事务。检索不获取此锁。
    with connect_database(importing=True) as coordinator:
        acquired=coordinator.execute('SELECT pg_try_advisory_lock(%s) AS acquired',(BUILD_LOCK,)).fetchone()['acquired']
        coordinator.commit()
        if not acquired:
            raise RuntimeError('另一个商品知识库构建正在运行，请稍后重试')
        try:
            with connect_database(importing=True) as conn:
                conn.execute((PROJECT_DIR/'schema_product_knowledge.sql').read_text())
            documents=source_documents()
            if not documents:
                raise RuntimeError('没有可向量化的商品描述，请先导入AdventureWorks数据')
            pending=_start_build(build_id,settings,documents)
            for offset in range(0,len(pending),batch_size):
                batch=pending[offset:offset+batch_size]
                vectors=client.embed_documents([d['content'] for d in batch])
                _write_batch(build_id,settings,batch,vectors)
                print(f'[向量化] {min(offset+batch_size,len(pending))}/{len(pending)}',flush=True)
            _publish_build(build_id,settings,documents)
        except BaseException:
            # 即使提交响应丢失，也不能把已经ready的构建降级为failed。
            try:
                with connect_database() as conn:
                    conn.execute("""UPDATE knowledge_builds SET status='failed',finished_at=CURRENT_TIMESTAMP
                        WHERE build_id=%s AND status='building'""",(build_id,))
            except Exception:
                pass  # 数据库失联可能保留building；活动指针仍由发布事务决定。
            raise
        finally:
            coordinator.execute('SELECT pg_advisory_unlock(%s)',(BUILD_LOCK,))
    return {'mode':'ready','build_id':build_id,'profile_id':settings.profile_id,
            'products':len(documents),'new_or_updated':len(pending),'reused':len(documents)-len(pending),
            'model':settings.public_configuration()['model'],'dimensions':settings.dimensions}


def main():
    parser=argparse.ArgumentParser(description='隔离构建商品向量并原子发布')
    parser.add_argument('--dry-run',action='store_true',help='只统计，不迁移或写入')
    parser.add_argument('--migrate-only',action='store_true',help='仅迁移已有索引，不计算Embedding')
    args=parser.parse_args()
    if args.dry_run and args.migrate_only:
        parser.error('--dry-run与--migrate-only不能同时使用')
    if args.migrate_only:
        initialize_knowledge_schema()
        result={'mode':'migrated'}
    else:
        result=build_knowledge(dry_run=args.dry_run)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
