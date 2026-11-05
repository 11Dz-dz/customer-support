"""Business tools; fixed identity checks and legacy read-only exports stay intact."""
import logging
from typing import Literal
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from adventureworks_service import AdventureWorksService
from catalog_filters import ProductCategory
from catalog_query import CatalogExpression
from tool_result import result_boundary,exception_result,failure_payload
from health_monitor import check_health
from session_identity import current_customer_id,SessionIdentityError
from chat_events import emit_event

order_service=AdventureWorksService()
logger=logging.getLogger(__name__)

def _business_tool(method, **arguments) -> dict:
    """工具仅调用预定义业务方法；日志不输出密钥、用户输入或完整订单。"""
    logger.info("tool_start name=%s", method.__name__)
    try:
        result = method(**arguments)
        logger.info("tool_end name=%s success=%s", method.__name__, result.get("success") if isinstance(result,dict) else None)
        return result
    except Exception as exc:
        logger.error("tool_error name=%s error_type=%s", method.__name__, type(exc).__name__)
        return exception_result(exc).model_dump(mode='json')


def _billing_tool(method, config: RunnableConfig, **arguments) -> dict:
    try:
        customer_id = current_customer_id(config)
    except SessionIdentityError as exc:
        emit_event("access_denied", config, tool=method.__name__, reason=exc.code)
        return exc.result()
    result = _business_tool(method, customer_id=customer_id, **arguments)
    if result.get("error_code") == "ORDER_NOT_ACCESSIBLE":
        emit_event("access_denied", config, tool=method.__name__, reason="ORDER_NOT_ACCESSIBLE")
    return result


@tool
@result_boundary
def check_system_status(component: Literal["database", "server", "api"]) -> dict:
    """实时只读健康检查，返回检测时间、范围、实际指标或失败原因。

    database仅检查PostgreSQL连接与SELECT 1；server检查当前容器CPU、内存及文件系统；
    api仅检查DeepSeek模型列表接口和本次鉴权，不验证对话生成、余额或所有端点。
    只在用户询问当前状态或明确请求检测时调用。解释错误码的一般含义时不需要调用。
    每轮当前状态必须重新检测，不用历史快照冒充实时结果，不支持任意URL或修复操作。
    """
    return check_health(component)


@tool
@result_boundary
def search_error_code(error_code: str) -> dict:
    """查询错误代码含义
    
    Args:
        error_code: 错误代码
    """
    import re
    value=str(error_code).strip().upper()
    parsed=re.fullmatch(r'(?:HTTP[\s:_-]*)?([1-5][0-9]{2})',value)
    error_code=parsed.group(1) if parsed else value
    error_db = {
        "500": "内部服务器错误 - 请联系技术团队",
        "404": "资源未找到 - 请检查URL",
        "403": "访问被拒绝 - 请检查权限",
        "401": "未授权 - 请先登录"
    }
    if error_code not in error_db:
        return failure_payload('ERROR_CODE_NOT_FOUND')
    return {'success':True,'text':f'{error_code}：{error_db[error_code]}','lookup_code':error_code}


@tool
@result_boundary
def query_products(keyword: str | None = None, min_price: float | None = None,
                   max_price: float | None = None,
                   sort_by: Literal["price", "name", "product_id"] = "price",
                   sort_order: Literal["asc", "desc"] = "asc", limit: int = 5,
                   operation: Literal["list", "count", "stats","overview","coverage"] = "list",
                   color: str | None = None, size: str | None = None,
                   include_zero_price: bool = False,product_category:ProductCategory|None=None,
                   catalog_filter:CatalogExpression|None=None,catalog_subject:Literal['catalog','transport']='catalog',
                   requested_item:str|None=None,search_terms:list[str]|None=None,source_text:str|None=None) -> dict:
    """精确查询整个商品目录：价格/颜色/尺寸筛选、排序、计数、价格统计。

    最高/最低价、前 N 名、预算筛选、数量和均价用本工具，不用语义搜索猜测。
    keyword 是名称/型号/中英文描述的词法条件，可用简短英文；不限定商品时传 null。
    sort_by 仅允许 price/name/product_id；sort_order 为 asc/desc；limit 为 1–20。
    operation=list 返回商品；count 返回匹配总数；stats 返回价格最小/最大/均值。
    color/size 是精确匹配字段值。默认排除 0 标价，不能把 0 当作免费商品。
    extreme_tie_count只表示最高/最低价位的并列数；boundary_price_tie_count表示列表末项价位的并列数。
    不同价位的并列数不能混用。结果含描述，足够回答时不重复查详情。
    """
    return _business_tool(order_service.query_products, keyword=keyword, min_price=min_price,
                          max_price=max_price, sort_by=sort_by, sort_order=sort_order,
                          limit=limit, operation=operation, color=color, size=size,
                          include_zero_price=include_zero_price,product_category=product_category,
                          catalog_filter=catalog_filter,catalog_subject=catalog_subject,requested_item=requested_item,search_terms=search_terms,source_text=source_text)


@tool
@result_boundary
def search_products(query: str, limit: int = 20, min_price: float | None = None,
                    max_price: float | None = None, product_category: ProductCategory | None = None,
                    keyword_terms:list[str]|None=None,color:str|None=None,size:str|None=None,
                    catalog_filter:CatalogExpression|None=None) -> dict:
    """检索商品名称和中英文描述。用于按需求找商品、比较型号及商品推荐。

    中文问题可以转换成简短英文关键词再检索，如山地车用 mountain bike。
    用于语义需求/用途推荐，不用于全目录最高价、最低价、排名或统计，这些用 query_products。
    最多先检索一次；明确不满足需求时再换关键词一次，禁止批量猜测多个类别或型号。
    预算必须填 min_price/max_price，会在两路召回前过滤；不得补造搜索结果。
    product_category为all不限、bicycles整车、components零部件、clothing服装、accessories装备。
    类别在两路召回前过滤；必须沿用当前任务的对象类别，不能删掉条件来搜索配件充当整车。
    min_price/max_price/product_category/color/size必须与当前任务一致；未限定的条件不要自行增加。
    不一致的工具请求会被阻止，原始调用记录不会被程序改写。已有种子检索足够时直接选择，不重复检索。

    Args:
        query: 商品名、型号或简短的检索关键词
        limit: 返回候选池数量，最多30；不是用户最终展示数量，通常20
        keyword_terms: 同一需求的简短中英文等价检索表达，不要加入未指定的类别
        min_price: 最低目录标价，包含边界
        max_price: 最高目录标价，包含边界
    """
    return _business_tool(order_service.search_products, query=query, limit=limit,
                          min_price=min_price, max_price=max_price, product_category=product_category,
                          keyword_terms=keyword_terms,color=color,size=size,catalog_filter=catalog_filter)


@tool
@result_boundary
def get_product_info(product_name: str) -> dict:
    """查询商品 ID、名称、型号、规格、描述和目录标价；商品与订单可对应。

    Args:
        product_name: 商品 ID（例如 776）、完整名称或商品型号
    """
    return _business_tool(order_service.get_product, product_name=product_name)


@tool
@result_boundary
def query_product_variants(product_id: StrictInt, axis: Literal['color','size'], strict: bool=False,
                           min_price:float|None=None,max_price:float|None=None,
                           requested_value:str|None=None,other_value:str|None=None,limit:int=20,
                           price_relation:Literal['lt','le','gt','ge']|None=None)->dict:
    """按已确认商品的真实product_model_id查询同款颜色或尺寸，保留另一规格。

    inform模式展示资料并标注原价格条件；strict模式仅返回符合价格条件的款式。
    只用于product_variants任务，不能通过语义召回或猜测名称替代型号关联；不表示实时库存。
    """
    return _business_tool(order_service.get_variants,product_id=product_id,axis=axis,strict=strict,
        min_price=min_price,max_price=max_price,requested_value=requested_value,other_value=other_value,limit=limit,price_relation=price_relation)


@tool
@result_boundary
def check_promotion(product: str) -> dict:
    """检查优惠数据是否可用。当前核心文件未包含优惠表，不能编造折扣。

    Args:
        product: 产品名称
    """
    return _business_tool(order_service.get_promotion, product_name=product)


class BundleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: StrictInt = Field(gt=0, le=2147483647)
    quantity: StrictInt = Field(ge=1, le=10000)


class ProductBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BundleItem] = Field(min_length=1, max_length=20)


@tool
@result_boundary
def calculate_price(product: str | None = None, quantity: StrictInt | None = None,
                    bundles: list[ProductBundle] | None = None, budget: str | None = None) -> dict:
    """按目录标价计算单商品或多商品组合报价，不创建订单。

    单商品使用product和quantity；多组备选方案使用bundles，每组包含items。
    例如bundles=[{"items":[{"product_id":915,"quantity":1},{"product_id":946,"quantity":1}]}]。
    一次可计算1–5组方案。价格由数据库读取，禁止填写单价或合计；budget可填"500"。
    金额、组合清单及预算比较以本工具返回为准，不能自己估算替代。

    Args:
        product: 单商品ID、名称或型号，与bundles二选一
        quantity: 单商品购买件数
        bundles: 多组独立备选方案，每组包含product_id和quantity组成的items
        budget: 每组方案的预算上限，十进制字符串；仅用于组合模式
    """
    if bundles is not None:
        if product is not None or quantity is not None:
            return failure_payload('INVALID_TOOL_ARGUMENTS')
        return _business_tool(order_service.calculate_bundles,
                              bundles=[b.model_dump() if isinstance(b, ProductBundle) else b for b in bundles], budget=budget)
    if product is None or quantity is None or budget is not None:
        return failure_payload('INVALID_TOOL_ARGUMENTS')
    return _business_tool(order_service.calculate_price, product_name=product, quantity=quantity)


@tool
@result_boundary
def query_my_orders(config:RunnableConfig,refund_filter:Literal['all','active','pending','approved','rejected','cancelled']|None=None,
                    limit:int=5)->dict:
    """按当前可信客户身份查询自己的订单候选，无需用户先知道订单号。

    refund_filter为空查近期订单，active查待审核/审核通过的申请，all查所有有退款记录的订单。
    列表只代表已保存的申请/订单状态，不代表正在执行资金退款。身份由系统提供，不接收客户编号。
    """
    return _billing_tool(order_service.list_my_orders,config,refund_filter=refund_filter,limit=limit)


@tool
@result_boundary
def query_order(order_id: str, config: RunnableConfig) -> dict:
    """查询订单状态、成交金额、发货日期及对应商品的名称、规格和描述。

    Args:
        order_id: AdventureWorks 数字订单号，例如 43659 或 SO43659
    """
    return _billing_tool(order_service.get_order, config, order_id=order_id)


@tool
@result_boundary
def query_invoice(order_id: str, config: RunnableConfig) -> dict:
    """查询订单账单摘要。

    数据集没有独立发票记录，仅返回订单金额摘要及发票数据缺失说明。

    Args:
        order_id: 数字订单号，例如 43659 或 SO43659
    """
    return _billing_tool(order_service.get_invoice, config, order_id=order_id)


@tool
@result_boundary
def check_payment_status(order_id: str, config: RunnableConfig) -> dict:
    """检查订单支付数据可用性。没有支付流水时返回 unknown，不推断支付成功。

    Args:
        order_id: 数字订单号，例如 43659 或 SO43659
    """
    return _billing_tool(order_service.get_payment, config, order_id=order_id)


@tool
@result_boundary
def request_refund(order_id: str, reason: str, config: RunnableConfig) -> dict:
    """已停用的历史退款工具。聊天不再提交申请。

    Args:
        order_id: 数字订单号，例如 43659 或 SO43659
        reason: 退款原因
    """
    # Kept only to decode/test legacy references; never writes, even if invoked directly.
    return failure_payload("CHAT_WRITE_NOT_ALLOWED")


@tool
@result_boundary
def query_refund_status(order_id: str, config: RunnableConfig) -> dict:
    """只读查询订单最新退款申请、审核状态和审核说明；不提交申请，不执行资金退款。"""
    return _billing_tool(order_service.get_refund_status, config, order_id=order_id)
