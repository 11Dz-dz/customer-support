# 样例数据准备

当前使用微软 AdventureWorks 虚构订单及商品资料，源码仓库不包含原始数据文件。来源记录在 [audit_summary.json](dataset_audit/audit_summary.json)，已有文件摘要在 [sha256.json](dataset_audit/sha256.json)。

准备以下五份原文件，放入本目录的 `dataset_audit/`：

- Product.csv
- ProductDescription.csv
- ProductModelProductDescriptionCulture.csv
- SalesOrderHeader.csv
- SalesOrderDetail.csv

来源为 [Microsoft sql-server-samples / AdventureWorks OLTP install script](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/adventure-works/oltp-install-script)。遵守来源项目对数据及文件的许可要求，本仓库不重新授权第三方数据。

文件扩展名虽为csv，当前导入器实际按UTF-8、无表头TSV读取。不要用Excel另存后直接替换，内容与现有sha256.json不一致时校验会拒绝；若采用不同来源版本，应先核对字段结构和数据一致性。

在项目根目录运行 `python import_adventureworks.py --validate-only` 验证，再运行普通导入。`instawdb.sql`是SQL Server脚本，当前PostgreSQL导入流程不执行它。不要把真实客户订单或数据库导出提交到仓库。
