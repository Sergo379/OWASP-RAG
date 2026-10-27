# План следующих этапов

Этапы №1–2 приняты пользователем. Этап №3 реализован по отдельному заданию; фактические результаты — STAGE3_RESULTS.md, самостоятельная приёмка — MANUAL_TESTING_STAGE3.md. Этап №4 ещё не выполняется.

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

- Ollama HTTP API, закреплённые `qwen3:1.7b` / `qwen3:4b`; реальные CPU inference, последовательный запуск, фильтрация thinking. Текущий 4B tag — Thinking-2507; для практичного альтернативного Chat отдельно подготовлен официальный `qwen3:4b-instruct-2507-q4_K_M`. Его имя фиксируется явно, результаты не смешиваются с Thinking.
- Prompt: System + User Query + Context; citations, проверка ответа и корректный отказ при недостатке источников.
- Настоящий AI Chat с результатами генерации.
- Langfuse 5 / OTel: реальная иерархия query / embedding / retrieval / filtering / reranking / context / generation / validation / final answer. Cloud-проверка BLOCKED без ключей пользователя; SDK/exporter тестируется локально.
- IDs документов/chunks, scores, Top-K, model/prompt/snapshot/config, inputs/outputs, timing/errors; локальный fallback при недоступности Langfuse.
- Актуальный Python SDK по [официальной документации](https://langfuse.com/docs/observability/sdk/overview), без обязательного LangChain.

## Этап №4 — Research & Evaluation

- 40 вопросов RU/EN: 10 development, 30 независимых final; эталонные ответы и релевантные документы.
- Сравнение четырёх стратегий, размеров 128/256/384, overlap 0/10/20%, двух embedding-моделей, Top-K, thresholds, reranking, двух LLM/режимов и двух prompt-стратегий.
- Recall@K, MRR, Hit Rate, Faithfulness, Answer Correctness, отказ при недостатке информации; latency/RAM.
- Только реальные измерения: CSV/JSON, графики, отчёт, сохранённые revisions/config/snapshot и связь результатов с Langfuse traces.
- Разделить tuning и final evaluation, не выбирать параметры по final set. Не объявлять лучший вариант до измерений.

Компоненты этапов №2–3 используют настоящие weights, ChromaDB и Ollama; этап №4 не реализован. Counts, фиксированные demo questions и timings — технические проверки, не итоговая метрика качества RAG.
