"""CPU mean pooling matching both official model cards; raw logits for cross-encoder."""

import gc
import time
from contextlib import contextmanager

import numpy as np

from src.core.config import AppError
from src.storage.files import now, write_json


def validate_vectors(vectors, dimension: int) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] != dimension or not np.isfinite(array).all():
        raise AppError("Модель вернула некорректную размерность или non-finite векторы")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if (norms <= 1e-12).any():
        raise AppError("Модель вернула нулевой вектор")
    return array / norms


class HFProvider:
    def __init__(self, spec, folder, threads: int = 4):
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

        started = time.perf_counter()
        torch.set_num_threads(threads)
        self.spec, self.model_identity = spec, spec.identity
        self.dimension, self.max_input_tokens = spec.dimension, spec.max_input_tokens
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(folder), local_files_only=True, trust_remote_code=False, use_fast=True
            )
            model_class = AutoModel if spec.kind == "embedding" else AutoModelForSequenceClassification
            self.model = (
                model_class.from_pretrained(
                    str(folder),
                    local_files_only=True,
                    trust_remote_code=False,
                    use_safetensors=True,
                    attn_implementation="eager",
                )
                .to("cpu")
                .eval()
            )
        except Exception as exc:
            raise AppError(
                "Не удалось загрузить локальную модель. Выполните Prepare Model и проверьте кеш/зависимости"
            ) from exc
        self.load_ms = round((time.perf_counter() - started) * 1000, 2)

    def input_count(self, text: str, query: bool = False) -> int:
        prefix = self.spec.query_prefix if query else self.spec.passage_prefix
        return len(self.tokenizer.encode(prefix + text, add_special_tokens=True, truncation=False))

    def document_parts(self, text: str, policy: str = "split") -> list[dict]:
        """Offsets refer to original chunk characters, preserving every character."""
        result, start = [], 0
        while start < len(text):
            remaining = text[start:]
            count = self.input_count(remaining)
            if count <= self.max_input_tokens:
                end = len(text)
            elif policy == "error":
                raise AppError(
                    f"Token budget exceeded: {count}>{self.max_input_tokens}; выберите overflow_policy=split"
                )
            else:
                offsets = self.tokenizer(
                    remaining, add_special_tokens=False, truncation=False, return_offsets_mapping=True
                )["offset_mapping"]
                budget = max(1, self.max_input_tokens - self.input_count(""))
                end = start + offsets[min(budget, len(offsets) - 1)][0]
                if end <= start:
                    end = start + 1
                while end > start and self.input_count(text[start:end]) > self.max_input_tokens:
                    end -= 1
                if end <= start:
                    raise AppError("Невозможно разбить chunk в пределах token budget модели")
            part = text[start:end]
            if part.strip():
                result.append(
                    {
                        "text": part,
                        "char_start": start,
                        "char_end": end,
                        "model_input_tokens": self.input_count(part),
                    }
                )
            start = end
        return result

    def _embed(self, texts: list[str], query: bool) -> np.ndarray:
        import torch

        if any(self.input_count(text, query) > self.max_input_tokens for text in texts):
            raise AppError(
                f"Превышен token budget {self.max_input_tokens}; сократите запрос или примените split"
            )
        prefix = self.spec.query_prefix if query else self.spec.passage_prefix
        inputs = self.tokenizer(
            [prefix + text for text in texts], padding=True, truncation=False, return_tensors="pt"
        )
        with torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
        return validate_vectors(pooled.cpu().numpy(), self.dimension)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, query=False)

    def embed_query(self, query: str) -> np.ndarray:
        return self._embed([query], query=True)[0]

    def rerank(
        self, query: str, texts: list[str], batch_size: int, passage_tokens: int
    ) -> tuple[list[float], list[dict]]:
        import torch

        query_count = len(self.tokenizer.encode(query, add_special_tokens=False, truncation=False))
        if query_count > 128:
            raise AppError("Запрос для reranker превышает 128 токенов; сократите его")
        pairs, owners, transformations = [], [], []
        # Explicit multi-window scoring, max raw logit across passages. No hidden truncation.
        for index, text in enumerate(texts):
            old_limit = self.max_input_tokens
            self.max_input_tokens = passage_tokens
            try:
                parts = self.document_parts(text)
            finally:
                self.max_input_tokens = old_limit
            transformations.append(
                {
                    "windows": len(parts),
                    "aggregation": "max_raw_logit",
                    "char_ranges": [[p["char_start"], p["char_end"]] for p in parts],
                }
            )
            for part in parts:
                ids = self.tokenizer(query, part["text"], truncation=False)["input_ids"]
                if len(ids) > self.max_input_tokens:
                    raise AppError("Превышен полный token budget пары reranker")
                pairs.append((query, part["text"]))
                owners.append(index)
        scores = [float("-inf")] * len(texts)
        for start in range(0, len(pairs), batch_size):
            batch = pairs[start : start + batch_size]
            inputs = self.tokenizer(
                [p[0] for p in batch],
                [p[1] for p in batch],
                padding=True,
                truncation=False,
                return_tensors="pt",
            )
            with torch.inference_mode():
                logits = self.model(**inputs).logits.squeeze(-1).cpu().numpy()
            if not np.isfinite(logits).all():
                raise AppError("Reranker вернул non-finite scores")
            for owner, score in zip(owners[start : start + batch_size], logits, strict=True):
                scores[owner] = max(scores[owner], float(score))
        return scores, transformations


class ModelManager:
    def __init__(self, files, factory=HFProvider):
        self.files, self.factory = files, factory
        self.loaded: dict[str, tuple[str, HFProvider]] = {}

    def get(self, key: str):
        spec = self.files.config.models.get(key)
        if spec is None:
            raise AppError("Неизвестная модель")
        slot = spec.kind
        if slot in self.loaded and self.loaded[slot][0] == key:
            return self.loaded[slot][1]
        self.unload(slot)
        if not self.files.ready(key, verify=True):
            write_json(
                self.files.folder(key) / "state.json",
                {
                    "status": "error",
                    "updated_at": now(),
                    "error": "Weights отсутствуют или не прошли SHA256; повторите Prepare Model",
                },
            )
            raise AppError(
                f"Модель {key} отсутствует или повреждена. Нажмите Prepare Model; для загрузки нужен интернет"
            )
        try:
            provider = self.factory(spec, self.files.folder(key), self.files.config.cpu_threads)
        except AppError as exc:
            write_json(
                self.files.folder(key) / "state.json",
                {"status": "error", "updated_at": now(), "error": str(exc)},
            )
            raise
        self.loaded[slot] = (key, provider)
        return provider

    def unload(self, slot: str | None = None):
        for name in list(self.loaded):
            if slot is None or slot == name:
                del self.loaded[name]
        gc.collect()

    @contextmanager
    def inference(self):
        # The application service holds its OS operation lock around all inference.
        yield self
