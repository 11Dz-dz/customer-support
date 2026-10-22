"""本地 CPU Embedding：multilingual-e5-small ONNX 量化模型。"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import threading

import numpy as np

from postgres_config import PROJECT_DIR, load_project_env


_client_lock = threading.Lock()


class EmbeddingError(RuntimeError):
    """可安全返回给检索层的本地模型错误。"""


@dataclass(frozen=True)
class EmbeddingSettings:
    model_path: str
    tokenizer_path: str
    dimensions: int = 384
    max_length: int = 512
    revision: str = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"

    @classmethod
    def from_env(cls):
        load_project_env()
        default_root = PROJECT_DIR / "models" / "multilingual-e5-small"
        root = Path(os.getenv("EMBEDDING_MODEL_PATH", str(default_root))).expanduser()
        model_path = root / "onnx" / "model_quantized.onnx"
        tokenizer_path = root / "tokenizer.json"
        if not model_path.is_file() or not tokenizer_path.is_file():
            raise EmbeddingError(f"本地 Embedding 模型不完整：{root}")
        try:
            max_length = int(os.getenv("EMBEDDING_MAX_LENGTH", "512"))
        except ValueError as exc:
            raise EmbeddingError("EMBEDDING_MAX_LENGTH 必须是正整数") from exc
        if not 8 <= max_length <= 512:
            raise EmbeddingError("EMBEDDING_MAX_LENGTH 必须在 8 到 512 之间")
        return cls(str(model_path), str(tokenizer_path), 384, max_length,
                   os.getenv("EMBEDDING_MODEL_REVISION", cls.revision))

    def public_configuration(self):
        return {
            "provider": "local_onnx_cpu",
            "model": "intfloat/multilingual-e5-small",
            "model_file": "onnx/model_quantized.onnx",
            "revision": self.revision,
            "dimensions": self.dimensions,
            "max_length": self.max_length,
            "document_version": 1,
        }

    @property
    def profile_id(self):
        raw = json.dumps(self.public_configuration(), sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()


def _validate_vectors(vectors: np.ndarray, expected_count: int, dimensions: int):
    if vectors.ndim != 2 or vectors.shape != (expected_count, dimensions):
        raise EmbeddingError("本地模型输出的向量维度不匹配")
    if not np.isfinite(vectors).all() or not np.any(vectors != 0, axis=1).all():
        raise EmbeddingError("本地模型输出了无效或零向量")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / norms).astype(np.float32)


class LocalEmbeddingClient:
    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            self._tokenizer = Tokenizer.from_file(settings.tokenizer_path)
            self._tokenizer.enable_truncation(max_length=settings.max_length)
            self._tokenizer.enable_padding(length=None, pad_id=1, pad_type_id=0, pad_token="<pad>")
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, int(os.getenv("EMBEDDING_CPU_THREADS", "1")))
            options.inter_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(settings.model_path, options, providers=["CPUExecutionProvider"])
        except ImportError as exc:
            raise EmbeddingError("缺少本地推理依赖，请安装 onnxruntime 和 tokenizers") from exc
        except Exception as exc:
            raise EmbeddingError("本地 Embedding 模型加载失败") from exc
        input_names = {item.name for item in self._session.get_inputs()}
        if not {"input_ids", "attention_mask", "token_type_ids"}.issubset(input_names):
            raise EmbeddingError("本地 ONNX 模型输入结构不兼容")

    def _encode(self, texts: list[str], prefix: str) -> list[list[float]]:
        from request_deadline import check_deadline, remaining_seconds
        check_deadline()
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("待向量化文本不能为空")
        encodings = self._tokenizer.encode_batch([prefix + text for text in texts])
        ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
        attention = np.asarray([item.attention_mask for item in encodings], dtype=np.int64)
        token_types = np.asarray([item.type_ids for item in encodings], dtype=np.int64)
        import onnxruntime as ort
        run_options = ort.RunOptions()
        remaining = remaining_seconds()
        timer = None
        if remaining is not None:
            check_deadline()
            timer = threading.Timer(remaining, lambda: setattr(run_options, 'terminate', True))
            timer.daemon = True
            timer.start()
        try:
            outputs = self._session.run(["last_hidden_state"], {
                "input_ids": ids, "attention_mask": attention, "token_type_ids": token_types,
            }, run_options=run_options)[0]
        except Exception:
            check_deadline()
            raise
        finally:
            if timer:
                timer.cancel()
        check_deadline()
        mask = attention.astype(np.float32)[..., None]
        pooled = (outputs * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1.0, None)
        return _validate_vectors(pooled, len(texts), self.settings.dimensions).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, "passage: ")

    @lru_cache(maxsize=128)
    def embed_query(self, query: str) -> tuple[float, ...]:
        return tuple(self._encode([query], "query: ")[0])


@lru_cache(maxsize=2)
def _client(settings: EmbeddingSettings):
    return LocalEmbeddingClient(settings)


def get_embedding_client():
    settings = EmbeddingSettings.from_env()
    # lru_cache 的并发冷启动可能重复构造实例；在缓存入口外锁住首次模型加载。
    with _client_lock:
        return _client(settings)


def vector_literal(vector) -> str:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise EmbeddingError("向量包含无效数值")
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"
