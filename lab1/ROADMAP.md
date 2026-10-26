# План следующих этапов

Этап №1 (Foundation & Data Pipeline) принят пользователем. Этап №2 реализует настоящий semantic retrieval; его самостоятельная приёмка описана в MANUAL_TESTING_STAGE2.md. Этапы №3–4 выполняются по отдельному заданию.

## Этап №2 — Embeddings & Vector Retrieval

Реализованная функциональность и фактические проверки: STAGE2_RESULTS.md. Ниже — область этапа, а не утверждение о результатах научной оценки.

- CPU embeddings: `intfloat/multilingual-e5-small` и `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` с закреплёнными revisions.
- Проверка полного token budget, prefixes и специальных токенов для каждой модели; без тихого усечения.
- ChromaDB persistent storage; отдельно идентифицируемые индексы для каждой модели, chunking config и snapshot.
- Собственный retrieval pipeline: Top-K 3/5/10/20, similarity/metadata filtering, deduplication.
- Опциональный CPU reranking: `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`.
- Рабочая страница Search с найденными chunks, sources, scores и вариантами с/без reranking.
- Последовательная загрузка моделей, небольшие батчи и измерение RAM.

## Этап №3 — Local LLM, UI & Langfuse

- Ollama, компактная квантованная Qwen3 1.7B; дополнительная Qwen3 4B Instruct для сравнения. Проверить актуальные имена и доступность тегов перед скачиванием.
- Prompt: System + User Query + Context; citations, проверка ответа и корректный отказ при недостатке источников.
- Настоящий AI Chat с результатами генерации.
- Обязательный Langfuse Cloud: полная иерархия query / embedding / retrieval / filtering / reranking / context building / generation / validation / final answer.
- IDs документов/chunks, scores, Top-K, model/prompt/snapshot/config, inputs/outputs, timing/errors; локальный fallback при недоступности Langfuse.
- Актуальный Python SDK по [официальной документации](https://langfuse.com/docs/observability/sdk/overview), без обязательного LangChain.

## Этап №4 — Research & Evaluation

- 40 вопросов RU/EN: 10 development, 30 независимых final; эталонные ответы и релевантные документы.
- Сравнение четырёх стратегий, размеров 128/256/384, overlap 0/10/20%, двух embedding-моделей, Top-K, thresholds, reranking, двух LLM/режимов и двух prompt-стратегий.
- Recall@K, MRR, Hit Rate, Faithfulness, Answer Correctness, отказ при недостатке информации; latency/RAM.
- Только реальные измерения: CSV/JSON, графики, отчёт, сохранённые revisions/config/snapshot и связь результатов с Langfuse traces.
- Разделить tuning и final evaluation, не выбирать параметры по final set. Не объявлять лучший вариант до измерений.

Компоненты этапа №2 используют настоящие weights и ChromaDB. Компоненты этапов №3–4 пока не установлены/не имитируются. Counts, demo queries и timings — технические проверки, не итоговая метрика качества RAG.
