from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from uuid import uuid4

from postgres_config import connect_database
from tool_result import failure_payload


ORDER_STATUSES = {1: "处理中", 2: "已批准", 3: "缺货待处理", 4: "已拒绝", 5: "已发货", 6: "已取消"}
SOURCE_NOTE = "微软 AdventureWorks 虚构历史样例，非实时店铺数据。"
CURRENCY_NOTE = "当前核心文件未提供可核实的币种标签，不假定人民币或美元。"
# Sales.SalesOrderHeader.DueDate: Date the order is due to the customer.
# microsoft/sql-server-samples / adventure-works / oltp-install-script / instawdb.sql
DATE_LABELS = {"purchase_at": "下单日期", "due_at": "订单约定交付日期", "shipped_at": "发货日期"}
REFUND_CAPABILITIES = {"register_application": True, "amend_application": False,
                       "attach_evidence": False, "review_notification": False}


def price_conditions(min_price=None, max_price=None, *, alias="", include_zero_price=False):
    """价格条件由精确 SQL 执行；0 标价默认视为无有效报价。"""
    values = []
    for value in (min_price, max_price):
        try:
            number = None if value is None else Decimal(str(value))
        except InvalidOperation:
            raise ValueError("价格必须为有限的非负数") from None
        if number is not None and (not number.is_finite() or number < 0):
            raise ValueError("价格必须为有限的非负数")
        values.append(number)
    lower, upper = values
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("最低价格不能超过最高价格")
    # alias 只能由调用代码提供，不能来自模型参数。
    column = alias + "list_price"
    clauses, parameters = [column + (" >= 0" if include_zero_price else " > 0")], []
    for operator, value in ((">=", lower), ("<=", upper)):
        if value is not None:
            clauses.append(f"{column} {operator} %s")
            parameters.append(value)
    return clauses, parameters


def keyword_conditions(keyword):
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clause = """(name ILIKE %s OR product_number ILIKE %s
                 OR coalesce(description_en,'') ILIKE %s
                 OR coalesce(description_zh,'') ILIKE %s
                 OR to_tsvector('english',coalesce(name,'') || ' ' || coalesce(description_en,''))
                    @@ plainto_tsquery('english',%s))"""
    return clause, [pattern, pattern, pattern, pattern, keyword]


def json_ready(value: Any):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_ready(v) for v in value]
    return value


def catalog_conditions(catalog_filter=None,*,min_price=None,max_price=None,product_category=None,color=None,size=None,include_zero_price=False,alias=''):
    from catalog_query import effective,sql
    # Validate legacy numeric bounds independently; never accept NaN or reversed bounds.
    price_conditions(min_price,max_price,include_zero_price=include_zero_price,alias=alias)
    value=effective(catalog_filter,min_price=min_price,max_price=max_price,product_category=product_category,color=color,size=size)
    clause,args=sql(value,alias=alias)
    return ([clause]+([] if include_zero_price else [alias+'list_price > 0'])),args


class AdventureWorksService:
    """订单与商品共享 PostgreSQL 数据；工具不生成任意 SQL。"""

    @staticmethod
    def _result(**values) -> dict:
        return json_ready({"success": True, "source_system": "adventureworks",
                           "source_note": SOURCE_NOTE, **values})

    @staticmethod
    def _order_id(value: str) -> int | None:
        text = str(value).strip().upper()
        if text.startswith("SO"):
            text = text[2:]
        if not re.fullmatch(r"[0-9]{1,10}", text):
            return None
        number = int(text)
        return number if 0 < number <= 2147483647 else None

    @staticmethod
    def _valid_customer(customer_id):
        return isinstance(customer_id, int) and not isinstance(customer_id, bool) and 0 < customer_id <= 2147483647

    @staticmethod
    def _unbound():
        return {"success": False, "error_code": "SESSION_UNBOUND", "error": "此会话尚未绑定客户，暂不能访问订单。"}

    def _find_order(self, conn, order_id: str, *, customer_id: int, lock: bool = False):
        number = self._order_id(order_id)
        if number is None:
            return None
        statement = "SELECT * FROM orders WHERE order_id=%s AND customer_id=%s"
        if lock:
            statement += " FOR UPDATE"
        return conn.execute(statement, (number, customer_id)).fetchone()

    @staticmethod
    def _not_found(order_id: str) -> dict:
        return {"success": False, "error_code": "ORDER_NOT_ACCESSIBLE", "error": "订单不存在或无权查看。"}

    def get_order(self, order_id: str, *, customer_id: int | None = None) -> dict:
        if not self._valid_customer(customer_id):
            return self._unbound()
        if self._order_id(order_id) is None:
            return self._not_found(order_id)
        with connect_database() as conn:
            order = self._find_order(conn, order_id, customer_id=customer_id)
            if order is None:
                return self._not_found(order_id)
            items = conn.execute(
                """SELECT i.order_item_id, i.product_id, p.name AS product_name,
                          p.product_number, p.color, p.size, p.description_en, p.description_zh,
                          i.quantity, i.unit_price, i.unit_price_discount, i.line_total,
                          i.tracking_number
                   FROM order_items i JOIN product_catalog p ON p.product_id=i.product_id
                   WHERE i.order_id=%s ORDER BY i.order_item_id""",
                (order["order_id"],),
            ).fetchall()
        return self._result(
            **order, status=ORDER_STATUSES.get(order["status_code"], "未知"), items=items,
            item_count=len(items), total_quantity=sum(i["quantity"] for i in items),
            currency=None, currency_note=CURRENCY_NOTE,
            date_labels=DATE_LABELS,
            date_semantics={"due_at": "delivery_due_date", "payment_due_at": "unknown"},
            delivery_note="shipped_at 是发货日期；未提供签收日期或实时物流轨迹。",
        )

    def list_my_orders(self,refund_filter=None,limit=5,*,customer_id:int|None=None)->dict:
        """Read only the authenticated customer's candidates, never a model-supplied identity."""
        if not self._valid_customer(customer_id):return self._unbound()
        allowed={None,'all','active','pending','approved','rejected','cancelled'}
        if not isinstance(refund_filter,(str,type(None))) or refund_filter not in allowed:
            return failure_payload('INVALID_QUERY_OPTIONS')
        if type(limit) is not int or not 1<=limit<=20:return failure_payload('INVALID_LIMIT')
        from refund_service import ORDER_REFUND_LABELS,FUNDS_NOTE
        with connect_database() as conn:
            rows=conn.execute('''SELECT o.order_id,o.order_number,o.purchase_at,o.status_code,o.total_due,
                    r.refund_id,r.refund_status,r.reason,r.review_note,count(*) OVER() AS matched_count
                FROM orders o LEFT JOIN LATERAL (
                    SELECT f.refund_id,f.status AS refund_status,f.reason,v.note AS review_note
                    FROM refund_requests f LEFT JOIN refund_reviews v USING(refund_id)
                    WHERE f.order_id=o.order_id
                    ORDER BY (f.status IN ('pending','approved')) DESC,f.created_at DESC,f.refund_id DESC LIMIT 1
                ) r ON true
                WHERE o.customer_id=%s AND (
                    %s::text IS NULL OR (%s='all' AND r.refund_status IS NOT NULL)
                    OR (%s='active' AND r.refund_status IN ('pending','approved')) OR r.refund_status=%s)
                ORDER BY o.order_id DESC LIMIT %s''',
                (customer_id,refund_filter,refund_filter,refund_filter,refund_filter,limit)).fetchall()
        count=rows[0]['matched_count'] if rows else 0
        for row in rows:
            row.pop('matched_count',None)
            row['fulfillment_status']=ORDER_STATUSES.get(row.pop('status_code'),'未知')
            row['refund_status_label']=ORDER_REFUND_LABELS.get(row['refund_status'])
        return self._result(orders=rows,matched_count=count,returned_count=len(rows),
            has_more=count>len(rows),refund_filter=refund_filter,currency=None,funds_note=FUNDS_NOTE)

    def get_payment(self, order_id: str, *, customer_id: int | None = None) -> dict:
        order = self.get_order(order_id, customer_id=customer_id)
        if not order.get("success"):
            return order
        return self._result(
            order_id=order["order_id"], order_number=order["order_number"],
            payment_status="unknown", order_total=order["total_due"], currency=None,
            note="源数据没有支付流水或网关状态；订单金额不是已支付金额，已发货也不等于已核实支付。",
        )

    def get_invoice(self, order_id: str, *, customer_id: int | None = None) -> dict:
        order = self.get_order(order_id, customer_id=customer_id)
        if not order.get("success"):
            return order
        return self._result(
            order_id=order["order_id"], order_number=order["order_number"],
            invoice_status="not_available", invoice_number=None,
            subtotal=order["subtotal"], tax_amount=order["tax_amount"], freight=order["freight"],
            order_total=order["total_due"], currency=None, currency_note=CURRENCY_NOTE,
            note="仅提供订单金额摘要。源数据没有独立发票记录，不能据此声称发票已开具。",
        )

    def query_products(self, keyword=None, min_price=None, max_price=None,
                       sort_by="price", sort_order="asc", limit=5, operation="list",
                       color=None, size=None, include_zero_price=False, product_category=None,catalog_filter=None,catalog_subject='catalog',requested_item=None,search_terms=None,source_text=None) -> dict:
        """全目录精确筛选、排序、计数及统计；不经过语义 Top K。"""
        if operation == 'coverage':
            from catalog_coverage import coverage
            return coverage(requested_item, search_terms, source_text)
        columns = {"price": "list_price", "name": "name", "product_id": "product_id"}
        if (sort_by not in columns or sort_order not in {"asc", "desc"}
                or operation not in {"list", "count", "stats", "overview"}):
            return failure_payload('INVALID_QUERY_OPTIONS')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            return failure_payload('INVALID_LIMIT')
        if not isinstance(include_zero_price, bool):
            return failure_payload('INVALID_TOOL_ARGUMENTS')
        if operation=='overview':
            return self.describe_catalog(catalog_filter=catalog_filter,catalog_subject=catalog_subject,
                min_price=min_price,max_price=max_price,product_category=product_category,color=color,size=size)
        try:
            clauses, parameters = catalog_conditions(catalog_filter,min_price=min_price,max_price=max_price,
                product_category=product_category,color=color,size=size,include_zero_price=include_zero_price)
        except ValueError as exc:
            return failure_payload('INVALID_PRICE_FILTER')
        for field, value in (("keyword", keyword),):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return failure_payload('INVALID_TOOL_ARGUMENTS')
            value = value.strip()
            if field == "keyword":
                clause, arguments = keyword_conditions(value)
                clauses.append(clause)
                parameters.extend(arguments)
            else:
                clauses.append(f"lower({field})=lower(%s)")
                parameters.append(value)
        where = " AND ".join(clauses)
        with connect_database() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            summary = conn.execute(
                f"""SELECT count(*) AS matched_count, min(list_price) AS min_price,
                           max(list_price) AS max_price, avg(list_price) AS average_price
                    FROM product_catalog WHERE {where}""", parameters,
            ).fetchone()
            products, tie_count, first_tie_count = [], None, None
            if operation == "list":
                # SQL 标识符均来自白名单，用户值始终参数化。
                order = columns[sort_by] + (" ASC" if sort_order == "asc" else " DESC")
                products = conn.execute(
                    f"SELECT * FROM product_catalog WHERE {where} ORDER BY {order}, product_id ASC LIMIT %s",
                    [*parameters, limit],
                ).fetchall()
                if products and sort_by == "price":
                    tie_count = conn.execute(
                        f"SELECT count(*) AS n FROM product_catalog WHERE {where} AND list_price=%s",
                        [*parameters, products[-1]["list_price"]],
                    ).fetchone()["n"]
                    first_tie_count = conn.execute(
                        f"SELECT count(*) AS n FROM product_catalog WHERE {where} AND list_price=%s",
                        [*parameters, products[0]["list_price"]],
                    ).fetchone()["n"]
        return self._result(
            operation=operation, query_scope="all_matching_catalog_products",
            filters={"keyword": keyword, "min_price": min_price, "max_price": max_price,
                     "color": color, "size": size, "include_zero_price": include_zero_price, "catalog_filter":catalog_filter.model_dump(mode="json") if hasattr(catalog_filter,"model_dump") else catalog_filter},
            sort_by=sort_by, sort_order=sort_order, limit=limit,
            matched_count=summary["matched_count"], products=products, returned_count=len(products),
            statistics=summary if operation == "stats" else None,
            boundary_price_tie_count=tie_count, currency=None, currency_note=CURRENCY_NOTE,
            extreme_kind=("min" if sort_order == "asc" else "max") if products and sort_by == "price" else None,
            extreme_price=products[0]["list_price"] if first_tie_count is not None else None,
            extreme_tie_count=first_tie_count,
            boundary_price=products[-1]["list_price"] if tie_count is not None else None,
            note="全量匹配范围内精确查询；默认排除标价为 0 的无有效报价项。结果为历史目录标价，非实时售价。"
                 "价格排序截断处可能有并列，不能把返回的一个型号说成唯一最高价或最低价商品。"
                 "keyword 是名称/型号/描述的词法匹配，不是语义匹配。",
        )

    def describe_catalog(self,catalog_filter=None,catalog_subject='catalog',**legacy):
        from catalog_query import effective,sql
        if catalog_subject not in {'catalog','transport'}:return failure_payload('INVALID_QUERY_OPTIONS')
        try:
            expr=effective(catalog_filter,**legacy);clause,args=sql(expr)
        except (ValueError,TypeError):return failure_payload('INVALID_TOOL_ARGUMENTS')
        from catalog_filters import CATEGORY_SUBCATEGORIES
        with connect_database() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            rows=conn.execute('SELECT product_subcategory_id,count(*) AS n FROM product_catalog WHERE '+clause+' GROUP BY 1',args).fetchall()
            unknown=conn.execute('SELECT count(*) AS n FROM product_catalog WHERE product_subcategory_id IS NULL').fetchone()['n']
        categories=[]
        for name,ids in CATEGORY_SUBCATEGORIES.items():
            if catalog_subject=='transport' and name!='bicycles':continue
            count=sum(r['n'] for r in rows if r['product_subcategory_id'] in ids)
            if count:categories.append({'category':name,'count':count})
        return self._result(operation='overview',catalog_subject=catalog_subject,categories=categories,
            unclassified_count=unknown,products=[],returned_count=0,matched_count=sum(r['count'] for r in categories),
            query_scope='classified_catalog_metadata',query_reason='catalog_overview',catalog_filter=expr.model_dump(mode='json'))

    def catalog_scope_summary(self,min_price=None,max_price=None,product_category=None,color=None,size=None,catalog_filter=None):
        clauses,parameters=catalog_conditions(catalog_filter,min_price=min_price,max_price=max_price,
            product_category=product_category,color=color,size=size)
        with connect_database() as conn:
            row=conn.execute(f"SELECT count(*) AS matched_count FROM product_catalog WHERE {' AND '.join(clauses)}",parameters).fetchone()
        return {'matched_count':row['matched_count'],'min_price':min_price,'max_price':max_price,
            'product_category':product_category,'color':color,'size':size,
            'catalog_filter':catalog_filter.model_dump(mode='json') if hasattr(catalog_filter,'model_dump') else catalog_filter}

    def search_products(self, query: str, limit: int = 20, min_price=None, max_price=None, product_category=None,
                        keyword_terms=None, color=None, size=None,catalog_filter=None) -> dict:
        query = str(query).strip()
        if not query or len(query) > 200:
            return failure_payload('INVALID_SEARCH_QUERY')
        limit = max(1, min(int(limit), 30))
        try:
            clauses,parameters=catalog_conditions(catalog_filter,min_price=min_price,max_price=max_price,
                product_category=product_category,color=color,size=size)
        except (ValueError,TypeError):return failure_payload('INVALID_TOOL_ARGUMENTS')
        from retrieval_pipeline import lexical_expression,search_phrases
        try:
            score,keyword_parameters=lexical_expression(query,keyword_terms)
            phrases=search_phrases(query,keyword_terms)
        except ValueError:return failure_payload('INVALID_SEARCH_QUERY')
        with connect_database() as conn:
            products = conn.execute(
                f"""SELECT product_id, name, product_number, product_model_id, product_subcategory_id, color, size, weight, weight_unit,
                          list_price, description_en, description_zh, sell_end_at, discontinued_at
                          , {score} AS lexical_score
                   FROM product_catalog
                   WHERE {' AND '.join(clauses)} AND {score}>0
                   ORDER BY lexical_score DESC, product_id
                   LIMIT %s""",
                [*keyword_parameters,*parameters,*keyword_parameters,limit],
            ).fetchall()
        result = self._result(query=query, products=products, returned_count=len(products),
                              retrieval_mode="keyword", semantic_available=False,keyword_terms=phrases,
                              currency=None, currency_note=CURRENCY_NOTE,
                              price_filters={"min_price": min_price, "max_price": max_price}, product_category=product_category,color=color,size=size,
                              note="价格为样例目录标价；排除 0 标价，缺少实时库存。没有描述的商品不能补造描述。")
        suspicious_query = (
            not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", query)
            or re.search(r"\b(?:or|and)\b\s+\d+\s*=\s*\d+|--|;", query, re.IGNORECASE)
        )
        try:
            if suspicious_query:
                return result
            from product_knowledge import hybrid_retrieve
            semantic = hybrid_retrieve(query, products, limit, min_price=min_price, max_price=max_price,
                                       product_category=product_category,color=color,size=size,catalog_filter=catalog_filter)
            result.update(semantic)
            result["returned_count"] = len(result["products"])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).info("semantic_retrieval_unavailable type=%s", type(exc).__name__)
        # 融合召回重新带入 PostgreSQL 的 Decimal/date；统一转成可解析的 JSON 值。
        result['catalog_filter']=catalog_filter.model_dump(mode='json') if hasattr(catalog_filter,'model_dump') else catalog_filter
        result['query_reason']='retrieved_candidates' if result['products'] else 'no_retrieval_candidates'
        return json_ready(result)

    def get_product(self, product_name: str) -> dict:
        value = str(product_name).strip()
        if re.fullmatch(r"[0-9]{1,10}", value) and int(value) <= 2147483647:
            with connect_database() as conn:
                product = conn.execute("SELECT * FROM product_catalog WHERE product_id=%s",
                                       (int(value),)).fetchone()
            if product:
                return self._result(product=product, currency=None, currency_note=CURRENCY_NOTE,
                                    inventory_status="unknown",
                                    note="目录标价不是订单成交价；未导入库存。停售及停产日期是历史字段，空值不代表当前在售或未停产。")
            return failure_payload('PRODUCT_NOT_FOUND')
        with connect_database() as conn:
            exact_rows = conn.execute(
                "SELECT product_id FROM product_catalog WHERE lower(name)=lower(%s) "
                "OR lower(product_number)=lower(%s) ORDER BY product_id LIMIT 2", (value, value),
            ).fetchall()
        if len(exact_rows) == 1:
            return self.get_product(str(exact_rows[0]["product_id"]))
        result = self.search_products(value)
        exact = [p for p in result.get("products", [])
                 if value.casefold() in (p["name"].casefold(), p["product_number"].casefold())]
        if len(exact) == 1:
            return self.get_product(str(exact[0]["product_id"]))
        if len(result.get("products", [])) == 1:
            return self.get_product(str(result["products"][0]["product_id"]))
        return {**result, "needs_selection": True,
                "message": "请根据候选商品确认名称或 ID。" if result.get("products") else "没有找到匹配商品，请换一个型号或英文关键词。"}

    def get_variants(self,product_id,axis,strict=False,min_price=None,max_price=None,
                     requested_value=None,other_value=None,limit=20,price_relation=None):
        if type(product_id) is not int or not 0<product_id<=2147483647 or axis not in {'color','size'} or type(limit) is not int or not 1<=limit<=20:
            return failure_payload('INVALID_TOOL_ARGUMENTS')
        price_conditions(min_price,max_price)  # finite, non-negative, ordered
        if price_relation not in {None,'lt','le','gt','ge'}:return failure_payload('INVALID_TOOL_ARGUMENTS')
        other='size' if axis=='color' else 'color'
        with connect_database() as conn:
            anchor=conn.execute('SELECT * FROM product_catalog WHERE product_id=%s',(product_id,)).fetchone()
            if not anchor:return failure_payload('PRODUCT_NOT_FOUND')
            if anchor['product_model_id'] is None:
                return self._result(anchor=anchor,variant_axis=axis,variant_status='model_unknown',strict=strict,
                    products=[],matched_count=0,returned_count=0,price_filters={'min_price':min_price,'max_price':max_price})
            fixed=other_value if other_value is not None else anchor[other]
            clauses=['product_model_id=%s',axis+' IS NOT NULL'];args=[anchor['product_model_id']]
            if fixed is not None:clauses.append(other+'=%s');args.append(fixed)
            if requested_value is not None:clauses.append(axis+'=%s');args.append(requested_value)
            elif anchor[axis] is not None:clauses.append(axis+'<>%s');args.append(anchor[axis])
            rows=conn.execute('SELECT * FROM product_catalog WHERE '+' AND '.join(clauses)+
                ' ORDER BY '+axis+',product_id',args).fetchall()
        lower=Decimal(str(min_price)) if min_price is not None else None
        upper=Decimal(str(max_price)) if max_price is not None else None
        selected=[];excluded=[]
        for row in rows:
            price=Decimal(str(row['list_price']))
            reference=Decimal(str(anchor['list_price']))
            relative_ok=(not price_relation or reference>0 and {'lt':price<reference,'le':price<=reference,
                'gt':price>reference,'ge':price>=reference}[price_relation])
            within=price>0 and relative_ok and (lower is None or price>=lower) and (upper is None or price<=upper)
            item={**row,'within_price':within,'price_known':price>0}
            if price>0 and Decimal(str(anchor['list_price']))>0:
                item['price_difference']=price-Decimal(str(anchor['list_price']))
            if strict and not within:excluded.append(item)
            else:selected.append(item)
        positive=[Decimal(str(p['list_price'])) for p in excluded if Decimal(str(p['list_price']))>0]
        return self._result(anchor=anchor,variant_axis=axis,variant_status='available' if rows else 'no_variants',
            fixed_spec={'field':other,'value':fixed},strict=strict,products=selected[:limit],matched_count=len(selected),
            returned_count=len(selected[:limit]),truncated=len(selected)>limit,
            excluded_summary={'count':len(excluded),'values':sorted({str(p[axis]) for p in excluded}),
                              'min_price':min(positive) if positive else None},
            price_filters={'min_price':min_price,'max_price':max_price},price_relation=price_relation,currency=None,currency_note=CURRENCY_NOTE)

    def get_promotion(self, product_name: str) -> dict:
        result = self.get_product(product_name)
        if not result.get("success") or "product" not in result:
            return result
        return self._result(product_id=result["product"]["product_id"], promotion_status="unknown",
                            note="当前上传文件不含优惠活动表，无法核实优惠；不能推断没有优惠或编造折扣。")

    def calculate_price(self, product_name: str, quantity: int) -> dict:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10000:
            return failure_payload('INVALID_QUANTITY')
        result = self.get_product(product_name)
        if not result.get("success") or "product" not in result:
            return result
        product = result["product"]
        if Decimal(product["list_price"]) <= 0:
            return failure_payload('PRODUCT_NOT_QUOTABLE')
        return self._result(product_id=product["product_id"], product_name=product["name"],
                            quantity=quantity, unit_price=product["list_price"],
                            subtotal=Decimal(product["list_price"]) * quantity, currency=None,
                            note="按样例目录标价估算，不包含运费、税费和未核实的折扣，不是正式下单。")

    def get_refund_status(self, order_id: str, *, customer_id: int | None = None) -> dict:
        """Read current application/review records, including applications created outside chat."""
        if not self._valid_customer(customer_id):return self._unbound()
        if self._order_id(order_id) is None:return self._not_found(order_id)
        from refund_service import application_rows,present
        with connect_database() as conn:
            order=self._find_order(conn,order_id,customer_id=customer_id)
            if order is None:return self._not_found(order_id)
            rows=[present(r) for r in application_rows(conn,order['order_id'])]
        return self._result(order_id=order['order_id'],refunds=rows,
                            funds_action='not_executed_by_this_system')

    def request_refund(self, order_id: str, reason: str, *, customer_id: int | None = None) -> dict:
        """Legacy trusted service adapter; never bound as an Agent tool or web endpoint."""
        if not self._valid_customer(customer_id):
            return self._unbound()
        reason = str(reason).strip()
        if not reason or len(reason) > 2000:
            return failure_payload('INVALID_REFUND_REASON')
        if self._order_id(order_id) is None:
            return self._not_found(order_id)
        with connect_database() as conn:
            # 订单行锁 + 部分唯一索引，确保并发申请不会重复登记。
            order = self._find_order(conn, order_id, customer_id=customer_id, lock=True)
            if order is None:
                return self._not_found(order_id)
            existing = conn.execute(
                "SELECT refund_id,status FROM refund_requests WHERE order_id=%s AND status IN ('pending','approved')",
                (order["order_id"],),
            ).fetchone()
            if existing:
                return self._result(order_id=order["order_id"], **existing, already_exists=True,
                                    operation="existing_application", capabilities=REFUND_CAPABILITIES,
                                    funds_action="not_executed_by_this_system",
                                    message="该订单已有未完成的退款申请；未执行资金退款。")
            refund_id = "RF-" + uuid4().hex[:12].upper()
            conn.execute("INSERT INTO refund_requests(refund_id,order_id,reason) VALUES (%s,%s,%s)",
                         (refund_id, order["order_id"], reason))
        return self._result(order_id=order["order_id"], refund_id=refund_id, status="pending",
                            already_exists=False, operation="created_application",
                            capabilities=REFUND_CAPABILITIES, funds_action="not_executed_by_this_system",
                            message="已登记待人工审核的售后申请；未判断退款资格，未执行资金退款。")

    def calculate_bundles(self, bundles: list, budget=None) -> dict:
        """一次报价多组可选方案；商品价格只从同一只读数据库快照取得。"""
        if not isinstance(bundles, list) or not 1 <= len(bundles) <= 5:
            return failure_payload('INVALID_BUNDLES')
        try:
            cap = None if budget is None else Decimal(str(budget))
            if cap is not None and (not cap.is_finite() or cap < 0):
                raise ValueError
        except (ValueError, InvalidOperation):
            return failure_payload('INVALID_BUDGET')
        normalized, ids = [], set()
        for bundle in bundles:
            if not isinstance(bundle, dict) or set(bundle) != {"items"}:
                return failure_payload('INVALID_BUNDLES')
            items = bundle["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                return failure_payload('INVALID_BUNDLES')
            merged = {}
            for item in items:
                if not isinstance(item, dict) or set(item) != {"product_id", "quantity"}:
                    return failure_payload('INVALID_BUNDLES')
                pid, qty = item["product_id"], item["quantity"]
                if (type(pid) is not int or not 0 < pid <= 2147483647
                        or type(qty) is not int or not 1 <= qty <= 10000):
                    return failure_payload('INVALID_BUNDLES')
                merged[pid] = merged.get(pid, 0) + qty
                if merged[pid] > 10000:
                    return failure_payload('INVALID_BUNDLES')
                ids.add(pid)
            normalized.append(merged)
        with connect_database() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute("SELECT product_id,name,product_number,list_price FROM product_catalog "
                                "WHERE product_id=ANY(%s)", (sorted(ids),)).fetchall()
        catalog = {row["product_id"]: row for row in rows}
        if set(catalog) != ids or any(row["list_price"] <= 0 for row in rows):
            return failure_payload('PRODUCT_NOT_QUOTABLE')
        results = []
        for index, bundle in enumerate(normalized, 1):
            items = [{"product_id": pid, "product_name": catalog[pid]["name"],
                      "product_number": catalog[pid]["product_number"], "quantity": qty,
                      "unit_price": catalog[pid]["list_price"],
                      "line_total": catalog[pid]["list_price"] * qty} for pid, qty in bundle.items()]
            subtotal = sum((item["line_total"] for item in items), Decimal(0))
            results.append({"bundle_index": index, "items": items, "subtotal": subtotal,
                            "within_budget": None if cap is None else subtotal <= cap})
        return self._result(bundles=results, budget=cap, currency=None, price_basis="historical_catalog",
                            note="各组为独立备选方案，按目录标价估算，未含税费、运费及未核实优惠；不创建订单。")
