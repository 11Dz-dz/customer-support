-- vector 扩展由管理员预先启用：CREATE EXTENSION vector WITH SCHEMA public;
-- 独立迁移，不修改原始订单和商品字段。
CREATE TABLE IF NOT EXISTS customer_service.knowledge_profiles (
    profile_id text PRIMARY KEY,
    configuration jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS customer_service.product_knowledge (
    profile_id text NOT NULL REFERENCES customer_service.knowledge_profiles(profile_id),
    product_id integer NOT NULL REFERENCES customer_service.products(product_id),
    content text NOT NULL,
    content_hash text NOT NULL,
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions > 0),
    embedding public.vector NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_id, product_id),
    CHECK (public.vector_dims(embedding) = embedding_dimensions)
);
CREATE TABLE IF NOT EXISTS customer_service.knowledge_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    active_profile_id text NOT NULL REFERENCES customer_service.knowledge_profiles(profile_id),
    activated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- profile是模型配置；build才是不可变的发布版本。
CREATE TABLE IF NOT EXISTS customer_service.knowledge_builds (
    build_id text PRIMARY KEY,
    profile_id text NOT NULL REFERENCES customer_service.knowledge_profiles(profile_id),
    status text NOT NULL CHECK (status IN ('building','ready','failed')),
    source_hash text NOT NULL,
    expected_count integer NOT NULL CHECK (expected_count >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz,
    UNIQUE (build_id,profile_id)
);
CREATE TABLE IF NOT EXISTS customer_service.knowledge_build_products (
    build_id text NOT NULL REFERENCES customer_service.knowledge_builds(build_id),
    product_id integer NOT NULL REFERENCES customer_service.products(product_id),
    content text NOT NULL,
    content_hash text NOT NULL,
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions > 0),
    embedding public.vector NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (build_id,product_id),
    CHECK (public.vector_dims(embedding) = embedding_dimensions)
);
ALTER TABLE customer_service.knowledge_settings ADD COLUMN IF NOT EXISTS active_build_id text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname='knowledge_settings_active_build_fk'
                     AND conrelid='customer_service.knowledge_settings'::regclass) THEN
        ALTER TABLE customer_service.knowledge_settings
            ADD CONSTRAINT knowledge_settings_active_build_fk
            FOREIGN KEY (active_build_id,active_profile_id)
            REFERENCES customer_service.knowledge_builds(build_id,profile_id);
    END IF;
END $$;
-- 已有活动索引在同一事务内复制为legacy构建，不重新Embedding，不覆盖原表。
INSERT INTO customer_service.knowledge_builds
    (build_id,profile_id,status,source_hash,expected_count,finished_at)
SELECT 'legacy-'||s.active_profile_id,s.active_profile_id,'ready','legacy',
       (SELECT count(*) FROM customer_service.product_knowledge p
        WHERE p.profile_id=s.active_profile_id),CURRENT_TIMESTAMP
FROM customer_service.knowledge_settings s WHERE s.active_build_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO customer_service.knowledge_build_products
    (build_id,product_id,content,content_hash,embedding_dimensions,embedding,updated_at)
SELECT 'legacy-'||p.profile_id,p.product_id,p.content,p.content_hash,
       p.embedding_dimensions,p.embedding,p.updated_at
FROM customer_service.product_knowledge p
JOIN customer_service.knowledge_settings s ON s.active_profile_id=p.profile_id
WHERE s.active_build_id IS NULL
ON CONFLICT DO NOTHING;
UPDATE customer_service.knowledge_settings
SET active_build_id='legacy-'||active_profile_id WHERE active_build_id IS NULL;
-- product_knowledge从此只用于旧数据迁移。当前小目录使用精确余弦检索。
