-- AdventureWorks 客服所需的最小业务结构。源文件是微软虚构示例数据。
-- 不运行 SQL Server 的 instawdb.sql；不删除已有 schema 或业务记录。
CREATE SCHEMA IF NOT EXISTS customer_service;
SET LOCAL search_path TO customer_service, public;

CREATE TABLE IF NOT EXISTS products (
    product_id integer PRIMARY KEY,
    name text NOT NULL,
    product_number text NOT NULL UNIQUE,
    color text,
    list_price numeric(19,4) NOT NULL CHECK (list_price >= 0),
    size text,
    weight numeric(10,2),
    weight_unit text,
    product_subcategory_id integer,
    product_model_id integer,
    sell_start_at timestamp,
    sell_end_at timestamp,
    discontinued_at timestamp
);
CREATE TABLE IF NOT EXISTS product_descriptions (
    description_id integer PRIMARY KEY,
    description text NOT NULL
);
CREATE TABLE IF NOT EXISTS product_model_descriptions (
    product_model_id integer NOT NULL,
    description_id integer NOT NULL REFERENCES product_descriptions(description_id),
    culture text NOT NULL,
    PRIMARY KEY (product_model_id, culture)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id integer PRIMARY KEY,
    order_number text NOT NULL UNIQUE,
    customer_id integer NOT NULL,
    status_code smallint NOT NULL CHECK (status_code BETWEEN 1 AND 6),
    purchase_at timestamp NOT NULL,
    due_at timestamp NOT NULL,
    shipped_at timestamp,
    ship_method_id integer,
    subtotal numeric(19,4) NOT NULL,
    tax_amount numeric(19,4) NOT NULL,
    freight numeric(19,4) NOT NULL,
    total_due numeric(19,4) NOT NULL,
    currency_rate_id integer
);
CREATE TABLE IF NOT EXISTS order_items (
    order_item_id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders(order_id),
    product_id integer NOT NULL REFERENCES products(product_id),
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(19,4) NOT NULL CHECK (unit_price >= 0),
    unit_price_discount numeric(10,4) NOT NULL CHECK (unit_price_discount BETWEEN 0 AND 1),
    line_total numeric(25,6) NOT NULL,
    tracking_number text
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(product_model_id);
CREATE INDEX IF NOT EXISTS idx_products_lower_name ON products(lower(name));

CREATE OR REPLACE VIEW product_catalog AS
SELECT p.*, en.description AS description_en, zh.description AS description_zh
FROM products p
LEFT JOIN product_model_descriptions me
  ON me.product_model_id = p.product_model_id AND me.culture = 'en'
LEFT JOIN product_descriptions en ON en.description_id = me.description_id
LEFT JOIN product_model_descriptions mz
  ON mz.product_model_id = p.product_model_id AND mz.culture = 'zh-cht'
LEFT JOIN product_descriptions zh ON zh.description_id = mz.description_id;

CREATE TABLE IF NOT EXISTS refund_requests (
    refund_id text PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders(order_id),
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refund_active_order
  ON refund_requests(order_id) WHERE status IN ('pending','approved');

CREATE TABLE IF NOT EXISTS dataset_imports (
    source_name text PRIMARY KEY,
    file_hashes jsonb NOT NULL,
    row_counts jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
