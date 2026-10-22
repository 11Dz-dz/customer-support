# 本地检索模型准备

当前运行使用 `Xenova/multilingual-e5-small` 的量化ONNX版本，384维，CPU推理。它负责Embedding；对话和规划仍调用DeepSeek API。

仓库仅保留 [模型来源与摘要](multilingual-e5-small/manifest.json)，不提交权重或大分词文件。请从对应模型仓库准备：

```text
models/multilingual-e5-small/
├── onnx/model_quantized.onnx
├── tokenizer.json
└── config.json
```

来源：[Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small)，已验证文件修订号及SHA-256见manifest.json。遵守原模型许可。模型目录可由EMBEDDING_MODEL_PATH指定，模板默认使用上面的相对路径；从项目根目录运行。

文件齐全、数据导入与数据库扩展准备完成后，运行 `python build_product_knowledge.py`。查询与文档前缀、推理长度及profile切换由程序管理；不要把不同配置的向量直接混用。
