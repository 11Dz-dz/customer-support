"""隔离schema中的真实PostgreSQL事务测试；假Embedding，不调用模型API。"""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
import os
import unittest

import build_product_knowledge as builder
import product_knowledge as knowledge
from embedding_client import EmbeddingError
from postgres_config import connect_database


class KnowledgeBuildTest(unittest.TestCase):
    def setUp(self):
        self.schema='test_kbuild_'+uuid4().hex
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.folder=self.stack.enter_context(TemporaryDirectory())
        root=Path(__file__).resolve().parents[1]
        self.ddl=(root/'schema_product_knowledge.sql').read_text(encoding='utf-8').replace('customer_service',self.schema)
        Path(self.folder,'schema_product_knowledge.sql').write_text(self.ddl,encoding='utf-8')
        with connect_database() as conn:
            conn.execute((root/'schema_postgres.sql').read_text(encoding='utf-8').replace('customer_service',self.schema))
        self.addCleanup(self.cleanup_schema)
        self.stack.enter_context(patch.object(builder,'PROJECT_DIR',Path(self.folder)))
        self.stack.enter_context(patch.object(builder,'connect_database',self.connect))
        self.stack.enter_context(patch.object(knowledge,'connect_database',self.connect))
        self.stack.enter_context(patch.object(builder,'load_project_env'))
        self.stack.enter_context(patch.dict(os.environ,{'EMBEDDING_BATCH_SIZE':'1'}))
        self.settings=SimpleNamespace(profile_id='profile-test',dimensions=3,
            public_configuration=lambda:{'model':'test-embedding','dimensions':3})
        self.client=SimpleNamespace(settings=self.settings,
            embed_documents=Mock(side_effect=lambda texts:[[1.,0.,0.] for _ in texts]),
            embed_query=Mock(return_value=[1.,0.,0.]))
        self.stack.enter_context(patch.object(builder,'get_embedding_client',return_value=self.client))
        self.stack.enter_context(patch.object(knowledge,'get_embedding_client',return_value=self.client))
        with self.connect() as conn:
            for pid in [1,2,3]:
                conn.execute('INSERT INTO products(product_id,name,product_number,list_price,product_model_id) VALUES (%s,%s,%s,10,%s)',
                             (pid,'Bike '+str(pid),'P'+str(pid),pid))
                conn.execute('INSERT INTO product_descriptions VALUES (%s,%s)',(pid,'Old description '+str(pid)))
                conn.execute("INSERT INTO product_model_descriptions VALUES (%s,%s,'en')",(pid,pid))

    def connect(self,**kwargs):
        conn=connect_database(**kwargs)
        conn.execute('SET search_path TO '+self.schema+',public')
        return conn

    def cleanup_schema(self):
        # 唯一、由本测试生成的schema；不清理任何customer_service记录。
        with connect_database() as conn:
            conn.execute('DROP SCHEMA '+self.schema+' CASCADE')

    def state(self):
        with self.connect() as conn:
            pointer=conn.execute('SELECT active_profile_id,active_build_id FROM knowledge_settings').fetchone()
            rows=conn.execute('SELECT product_id,content,content_hash,embedding::text FROM knowledge_build_products WHERE build_id=%s ORDER BY product_id',
                              (pointer['active_build_id'],)).fetchall()
        return pointer,rows

    def change(self,*ids):
        with self.connect() as conn:
            for pid in ids:
                conn.execute('UPDATE product_descriptions SET description=%s WHERE description_id=%s',('New '+str(pid),pid))

    def test_embedding_failure_keeps_active_build_and_reader_isolated(self):
        builder.build_knowledge()
        before=self.state()
        self.change(1,2)
        calls=[]
        def embed(texts):
            calls.extend(texts)
            self.assertEqual(self.state(),before)
            if len(calls)==2:
                # 第一批新向量已提交，但查询只能看旧活动构建。
                result=knowledge.hybrid_retrieve('bike',[],5)
                self.assertEqual([p['product_id'] for p in result['products']],[3])
                raise EmbeddingError('injected failure')
            return [[1.,0.,0.]]
        self.client.embed_documents.side_effect=embed
        with self.assertRaises(EmbeddingError):builder.build_knowledge()
        self.assertEqual(self.state(),before)
        with self.connect() as conn:
            failed=conn.execute("SELECT build_id FROM knowledge_builds WHERE status='failed'").fetchone()['build_id']
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM knowledge_build_products WHERE build_id=%s',(failed,)).fetchone()['n'],2)
        self.client.embed_documents=Mock(side_effect=lambda texts:[[1.,0.,0.] for _ in texts])
        result=builder.build_knowledge()
        self.assertEqual((result['reused'],result['new_or_updated']),(2,1))
        self.assertEqual(self.client.embed_documents.call_count,1)
        self.assertNotEqual(self.state()[0]['active_build_id'],before[0]['active_build_id'])
        self.assertEqual(len(knowledge.hybrid_retrieve('bike',[],5)['products']),3)

    def test_unchanged_documents_reuse_vectors_into_new_build(self):
        first=builder.build_knowledge()
        self.client.embed_documents.reset_mock()
        second=builder.build_knowledge()
        self.assertEqual(second['reused'],3)
        self.assertEqual(second['new_or_updated'],0)
        self.assertNotEqual(first['build_id'],second['build_id'])
        self.client.embed_documents.assert_not_called()

    def test_new_profile_failure_does_not_touch_old_build(self):
        builder.build_knowledge()
        before=self.state()
        self.settings.profile_id='profile-new'
        self.client.embed_documents.side_effect=EmbeddingError('injected')
        with self.assertRaises(EmbeddingError):builder.build_knowledge()
        self.assertEqual(self.state(),before)

    def test_source_changed_during_build_prevents_publication(self):
        builder.build_knowledge()
        before=self.state()
        self.change(1)
        def embed(texts):
            self.change(2)
            return [[1.,0.,0.]]
        self.client.embed_documents.side_effect=embed
        with self.assertRaisesRegex(RuntimeError,'描述发生变化'):builder.build_knowledge()
        self.assertEqual(self.state(),before)

    def test_publication_transaction_rolls_back_ready_status_on_failure(self):
        builder.build_knowledge()
        before=self.state()
        with self.connect() as conn:
            conn.execute("""CREATE FUNCTION reject_publish() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'injected publish failure'; END $$""")
            conn.execute('CREATE TRIGGER fail_pointer BEFORE UPDATE ON knowledge_settings FOR EACH ROW EXECUTE FUNCTION reject_publish()')
        with self.assertRaises(Exception):builder.build_knowledge()
        self.assertEqual(self.state(),before)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM knowledge_builds WHERE status='ready'").fetchone()['n'],1)

    def test_batch_database_error_rolls_back_entire_batch(self):
        builder.build_knowledge()
        before=self.state()
        self.change(1,2)
        with patch.dict(os.environ,{'EMBEDDING_BATCH_SIZE':'2'}):
            self.client.embed_documents.side_effect=lambda texts:[[1.,0.,0.],[1.,0.]]
            with self.assertRaises(Exception):builder.build_knowledge()
        self.assertEqual(self.state(),before)
        with self.connect() as conn:
            failed=conn.execute("SELECT build_id FROM knowledge_builds WHERE status='failed'").fetchone()['build_id']
            # 只保留复用的第三件；本批第一条插入也随第二条维度错误回滚。
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM knowledge_build_products WHERE build_id=%s',(failed,)).fetchone()['n'],1)

    def test_embedding_result_count_mismatch_is_not_published(self):
        builder.build_knowledge()
        before=self.state()
        self.change(1)
        self.client.embed_documents.side_effect=lambda texts:[]
        with self.assertRaisesRegex(EmbeddingError,'数量'):builder.build_knowledge()
        self.assertEqual(self.state(),before)

    def test_existing_legacy_index_migrates_idempotently_without_embedding(self):
        # 只安装旧DDL，构造旧活动表，再执行真实迁移。
        old=self.ddl.split('-- profile是模型配置')[0]
        with self.connect() as conn:
            conn.execute(old)
            conn.execute("INSERT INTO knowledge_profiles VALUES ('profile-test','{}',CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO product_knowledge(profile_id,product_id,content,content_hash,embedding_dimensions,embedding) VALUES ('profile-test',1,'old','hash',3,'[1,0,0]')")
            conn.execute("INSERT INTO knowledge_settings(singleton,active_profile_id) VALUES (true,'profile-test')")
        builder.initialize_knowledge_schema()
        before=self.state()
        builder.initialize_knowledge_schema()
        self.assertEqual(self.state(),before)
        self.assertEqual(before[0]['active_build_id'],'legacy-profile-test')
        self.assertEqual(len(before[1]),1)
        self.client.embed_documents.assert_not_called()

    def test_dry_run_performs_no_schema_write_or_embedding(self):
        self.assertEqual(builder.build_knowledge(dry_run=True)['eligible_products'],3)
        self.client.embed_documents.assert_not_called()
        with self.connect() as conn:
            self.assertIsNone(conn.execute('SELECT to_regclass(%s) AS name',(self.schema+'.knowledge_builds',)).fetchone()['name'])

    def test_concurrent_build_rejected_before_any_write(self):
        builder.build_knowledge()
        before=self.state()
        with connect_database() as lock:
            lock.execute('SELECT pg_advisory_lock(%s)',(builder.BUILD_LOCK,))
            lock.commit()
            try:
                with self.assertRaisesRegex(RuntimeError,'正在运行'):builder.build_knowledge()
            finally:
                lock.execute('SELECT pg_advisory_unlock(%s)',(builder.BUILD_LOCK,))
        self.assertEqual(self.state(),before)


if __name__=='__main__':
    unittest.main(verbosity=2)
