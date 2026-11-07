# 商品推荐检索与候选精选

本轮将“召回数量、模型可见候选、最终展示数量”分开，沿用现有本地Embedding、PostgreSQL与DS；没有安装或宣称使用新的cross-encoder重排模型。

## 问题来源

M05实际记录中，第二次检索找到了描述明确写“长途骑乘”的商品，但排第8；通用上下文压缩只保留前5个，因此模型看不到该商品。旧选择与任务完成逻辑还可能在模型清单无效时，按插入顺序填入首次候选，造成未经精选的商品被展示、记忆和后续报价引用。完整记录里也观察到评审漏读免责声明，不能把一次机器不通过直接等同业务事实错误。

## 当前流程

任务规划提取核心keyword、等价中英文search_terms、预算、类别和展示数量 → 推荐专用词法匹配＋向量召回 → RRF → 合并本轮多次检索并按商品ID去重 → 不同型号优先分布 → 在总体预算内构建短候选卡 → DS按需求选择排序并引用描述 → 程序验证ID、价格/类别范围、展示数量及变体策略 → 展示并保存实际选择。

1. `backend/tasks/task_state.py`新增search_terms与include_variants。复用已有DS规划，不另加重写调用。核心用途或对象变化时清除旧扩展词，预算变化可保留用途。仅用户明确要比较同型号颜色/尺寸时include_variants=true。
2. `backend/orders/adventureworks_service.py`推荐搜索使用独立的lexical_expression：短语匹配加分，英文有效词项提供较宽候选覆盖；等价表达不强制整句共同出现。所有值仍参数化。精确目录query_products继续使用原规则，不把计数和全目录排序改成宽泛语义匹配。
3. 推荐search_products最多返回30个候选，程序初次通常取20，显示数量仍由task.limit控制。向量路最多先取40，业务价格和类别过滤仍在召回前执行。记录两路候选数并保留模型ID。商品文档及Embedding不变，无须重建向量索引。
4. `backend/catalog/retrieval_pipeline.py`按本轮独立查询合并候选、去重，融合多个查询的名次；优先给不同型号留位置，完整变体仍可供明确比较。该排序是候选组织方式，不是已经证明用途适合的概率分数。
5. `backend/agent/context_budget.py`为推荐任务建立统一候选池：保留ID、名称、型号、类别、价格及中英文描述，删重复retrieved_content与不必要字段；多次工具结果在输入副本中引用同一池，避免重复占用。候选卡遵守总体字符预算，必要时从统一排序后的尾部省略，并记录原始/可见/省略ID。既不无限保留全部结果，也不对每次搜索机械截前5。历史与完整工具结果不变，模型原始工具调用配对和推理字段按原边界保护。
6. 沿用DS专家作为语义精选阶段：从统一候选中选择，不只因为相似、便宜就推荐；允许少于要求数量，默认控制同型号重复。原文解释仍由证据渲染校验，不能新增未经核实的性能事实。
7. 模型JSON无效、清单为空或有未知/不合范围ID时，返回no_recommendation，不按最早候选补足。任务记为empty（未确认适合的推荐）而非工具执行错误；临时故障仍遵循partial/failed。任务记忆只保存实际展示ID，避免未选中的候选进入后续报价。详细查询和订单商品记录的展示规则保持原用途。

## 观测和限制

context_prepared记录candidate_pool_ids、visible_candidate_ids、omitted_candidate_ids。recommendation_selected记录模型输入清单、最终选中ID、原因及是否未形成推荐，不记录自由文本推理或原始提示词。可定位“召回有没有找到、模型有没有看到、精选选了什么、程序做了什么限制”。LangSmith保留原有完整链路。

词法OR覆盖可能带入弱相关内容，需要DS根据具体描述筛选；这一版不是专用cross-encoder，也不保证每次语义判断正确。描述逐字引用能证明来源，不能单独证明满足用途。当前价格、类别、颜色、尺寸及其支持的包含/排除条件在两路召回前统一编译，并在候选及展示阶段核对；型号代表偏好不能替代硬筛选。详见[统一商品查询说明](CATALOG_QUERY_CONTRACT.md)。极端长描述仍可能让统一池超预算，省略会显式记录。重排、变体控制和扩大候选池的效果需要场景实测，不能仅因参考项目采用类似步骤就宣称质量提升。

## 参考来源

- [Dify重排源码](https://github.com/langgenius/dify/blob/main/api/core/rag/rerank/rerank_model.py)：重排、阈值、排序和Top N分阶段。本项目本轮没有引入其专用reranker模型。
- [Onyx搜索源码](https://github.com/onyx-dot-app/onyx/blob/main/backend/onyx/tools/tool_implementations/search/search_tool.py)：多查询融合、候选精选及预算约束；本项目借鉴任务分层，保留自己的工具协议和DS专家。
- [RAGFlow检索配置说明](https://github.com/infiniflow/ragflow/blob/main/docs/guides/agent/agent_workflow/basic_component.md#retrieval-component)：召回候选数量与下游Top N分开控制。
