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
-- 当前只有几百条商品资料，使用精确余弦近邻检索，无需近似索引。
-- 新模型在完整建库并验证后才切换 active_profile_id，避免混用不同模型的向量。
