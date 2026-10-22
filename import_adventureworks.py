"""校验并一次性导入已上传的五份 AdventureWorks TSV；不会清空旧数据。"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from postgres_config import PROJECT_DIR, connect_database


# filename -> (source column count, target table, target columns, source indexes)
TABLES = {
    "Product.csv": (25, "products", (
        "product_id", "name", "product_number", "color", "list_price", "size", "weight",
        "weight_unit", "product_subcategory_id", "product_model_id", "sell_start_at",
        "sell_end_at", "discontinued_at",
    ), (0, 1, 2, 5, 9, 10, 13, 12, 18, 19, 20, 21, 22)),
    "ProductDescription.csv": (4, "product_descriptions", (
        "description_id", "description",
    ), (0, 1)),
    "ProductModelProductDescriptionCulture.csv": (4, "product_model_descriptions", (
        "product_model_id", "description_id", "culture",
    ), (0, 1, 2)),
    "SalesOrderHeader.csv": (26, "orders", (
        "order_id", "order_number", "customer_id", "status_code", "purchase_at", "due_at",
        "shipped_at", "ship_method_id", "subtotal", "tax_amount", "freight", "total_due",
        "currency_rate_id",
    ), (0, 7, 10, 5, 2, 3, 4, 15, 19, 20, 21, 22, 18)),
    "SalesOrderDetail.csv": (11, "order_items", (
        "order_item_id", "order_id", "product_id", "quantity", "unit_price",
        "unit_price_discount", "line_total", "tracking_number",
    ), (1, 0, 4, 3, 6, 7, 8, 2)),
}
INTEGER_COLUMNS = {
    "product_id", "product_subcategory_id", "product_model_id", "description_id", "order_id",
    "customer_id", "status_code", "ship_method_id", "currency_rate_id", "order_item_id", "quantity",
}
DECIMAL_COLUMNS = {"list_price", "weight", "subtotal", "tax_amount", "freight", "total_due",
                   "unit_price", "unit_price_discount", "line_total"}


def read_source(path: Path, expected_columns: int):
    # Microsoft 的这些 .csv 实为无表头 TSV；引号是普通文本，不能按常规 CSV 引号解析。
    with path.open(encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) != expected_columns:
                raise ValueError(f"{path.name}:{line_no} 列数应为 {expected_columns}，实际 {len(fields)}")
            yield fields


def load_sources(data_dir: Path):
    data, hashes = {}, {}
    for filename, (width, table, columns, positions) in TABLES.items():
        path = data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"缺少数据文件：{path}")
        hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        records = []
        for fields in read_source(path, width):
            row = []
            for column, index in zip(columns, positions):
                value = fields[index].strip() or None
                if value is not None:
                    if column in INTEGER_COLUMNS:
                        value = int(value)
                    elif column in DECIMAL_COLUMNS:
                        value = Decimal(value)
                row.append(value)
            records.append(tuple(row))
        data[table] = records
    manifest = data_dir / "sha256.json"
    if manifest.exists():
        expected = json.loads(manifest.read_text(encoding="utf-8-sig"))
        for name, digest in hashes.items():
            if name in expected and expected[name] != digest:
                raise ValueError(f"{name} 校验和不一致，导入已终止")
    return data, hashes


def validate_sources(data: dict) -> dict:
    products = {r[0]: r for r in data["products"]}
    orders = {r[0]: r for r in data["orders"]}
    descriptions = {r[0] for r in data["product_descriptions"]}
    models = {(r[0], r[2]) for r in data["product_model_descriptions"]}
    for table, rows in data.items():
        keys = [(r[0], r[2]) if table == "product_model_descriptions" else r[0] for r in rows]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{table} 存在重复主键")
    for _, description_id, _ in data["product_model_descriptions"]:
        if description_id not in descriptions:
            raise ValueError("商品描述关联引用不存在的描述")
    for row in data["order_items"]:
        if row[1] not in orders or row[2] not in products:
            raise ValueError("订单明细存在悬空的订单或商品引用")
        if row[3] <= 0:
            raise ValueError("订单商品数量必须大于 0")
        expected = Decimal(row[3]) * row[4] * (1 - row[5])
        if abs(expected - row[6]) > Decimal("0.000001"):
            raise ValueError(f"订单明细 {row[0]} 的数量、折扣与金额不一致")
    return {
        "row_counts": {table: len(rows) for table, rows in data.items()},
        "order_status_counts": dict(Counter(r[3] for r in orders.values())),
        "ordered_products": len({r[2] for r in data["order_items"]}),
        "products_with_english_description": sum((p[9], "en") in models for p in products.values()),
        "note": "虚构历史样例；订单状态为源文件值，未补造支付、库存或退款结果。",
    }


def import_data(data_dir: Path, *, validate_only: bool = False) -> dict:
    data, hashes = load_sources(data_dir)
    report = validate_sources(data)
    if validate_only:
        return {"mode": "validate_only", **report}
    from psycopg import sql
    from psycopg.types.json import Jsonb

    with connect_database(importing=True) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(48151331)")
        conn.execute((PROJECT_DIR / "schema_postgres.sql").read_text(encoding="utf-8"))
        old = conn.execute("SELECT file_hashes, row_counts FROM dataset_imports WHERE source_name=%s",
                           ("adventureworks",)).fetchone()
        counts = {
            table: conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"]
            for table in data
        }
        if old:
            if old["file_hashes"] != hashes or counts != report["row_counts"]:
                raise RuntimeError("数据库已有不同版本或不完整的数据，已停止；请使用新的专用数据库导入。")
            return {"mode": "already_imported", **report}
        if any(counts.values()):
            raise RuntimeError("目标业务表非空且缺少导入记录，已停止，避免覆盖现有数据。")
        for _, (_, table, columns, _) in TABLES.items():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)),
            )
            with conn.cursor().copy(statement) as copy:
                for row in data[table]:
                    copy.write_row(row)
            print(f"[导入] {table}: {len(data[table])} 行")
        conn.execute("INSERT INTO dataset_imports(source_name,file_hashes,row_counts) VALUES (%s,%s,%s)",
                     ("adventureworks", Jsonb(hashes), Jsonb(report["row_counts"])))
    return {"mode": "imported", **report}


def main() -> None:
    parser = argparse.ArgumentParser(description="将 AdventureWorks 核心 TSV 导入 PostgreSQL")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR / "data" / "dataset_audit")
    parser.add_argument("--validate-only", action="store_true", help="只校验源数据，无需数据库或 psycopg")
    args = parser.parse_args()
    print(json.dumps(import_data(args.data_dir, validate_only=args.validate_only), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
