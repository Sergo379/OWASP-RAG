# Архитектура и расширение

```text
HTML/CSS/JS ─ HTTP ─ FastAPI ─┐
                            ├─ Application Service ─ Grabber ─ GitHub HTTP
CLI / Pixi ──────────────────┘          │              │
                                       ├─ Preprocessing (cleanup → E5 tokenizer → chunker)
                                       └─ File Store (raw, manifest, JSONL, operation history)
```

Мутации сериализованы OS FileLock по каталогу данных. API выполняет операции в одном фоне; endpoints продолжают отвечать. CLI синхронный. UI опрашивает состояние раз в две секунды. Состояние и история сохраняются, очередь задач/Redis не требуются. Фоновые операции не являются durable job queue: после аварийного завершения пользователь повторяет запуск. Проверенные raw checkpoints сохраняются.

Raw — адресуемые по SHA256 immutable bytes. Manifest — атомарный JSON-указатель на проверенные raw. JSONL публикуется полностью, затем атомарно меняется processed index. Прерванный preprocessing не меняет указатель на предыдущий успешный результат. Windows sharing violation при replace обрабатывается ограниченным повтором. Неиспользуемые blobs/JSONL остаются для воспроизводимости; автоматического удаления нет.

Идентичность документа: repository + path. Идентичность snapshot: commit + manifest map(document_id → raw SHA). Идентичность preprocessing: хеш полной конфигурации, включающей pipeline_version, tokenizer model/revision/digest и параметры chunking. Run хранит карту исходных SHA и дату построения. Идентичность chunk: документ, raw version, конфигурация, позиция, text SHA. После обновления кода preprocessing нужно менять pipeline_version, если меняется семантика обработки.

Скачанные Markdown — недоверенные данные. Они не импортируются, не исполняются, не преобразуются в исполняемый HTML. Имена файлов storage выводятся из хешей, а не из внешних путей. API доступен только на loopback, проверяет Host и Origin мутаций. Это локальный учебный инструмент; для сетевого размещения потребуется отдельный проект аутентификации/авторизации.

## Контракты следующих этапов (пока только проектирование)

| Компонент | Вход | Выход / provenance |
| --- | --- | --- |
| Embeddings | Chunk JSONL + embedding model/revision + prefixes | vectors, model id, размерность, chunk id; батчи CPU |
| Vector storage | vectors + Chunk metadata | отдельная persistent collection для snapshot × preprocessing × embedding model/revision |
| Retrieval | query, embedding model, collection id, Top-K | chunk IDs/text, similarity scores и длительность |
| Filtering | retrieval candidates + threshold/metadata | сохранённые/отброшенные кандидаты с причиной; dedup |
| Reranking | query + candidates + model/revision | исходный rank/score и новый rank/score |
| Context builder | filtered/reranked chunks + prompt budget | контекст, citations, source IDs; без тихого truncation |
| Generation | system prompt/version + query + context + Ollama model | answer, usage/timing, источники, отказ при недостатке контекста |
| Evaluation | независимые вопросы, expected documents/answers, config/snapshot | реальные CSV/JSON, Recall@K/MRR/Hit Rate и качество ответа |
| Observability | события каждого шага с одним request/trace id | локальные логи + Langfuse hierarchy, независимый failure path |

Схемы Document/Chunk в `src/core/models.py` — действующие контракты этапа №1. Не вводим пустые классы будущих сервисов, пока не определены реальные SDK. На этапе №2 индексатор обязан проверять длину полного ввода целевым токенизатором. Размеры текущих chunks оставляют резерв для E5; токенизация второй модели должна быть проверена отдельно.

На этапе №3 Langfuse Cloud обязателен: query → embedding → retrieval → filtering → reranking → context → generation → validation → final answer. Для каждого span — inputs/outputs, параметры, timing/errors; отдельно candidate IDs, scores до/после reranking, Top-K, snapshot, LLM и prompt version. Проверить актуальный официальный SDK перед реализацией. Ошибка observability не должна ломать основной RAG.

Компоненты можно позднее выделить в сервисы лабораторной №2, сохранив API-границы application layer. Сейчас один процесс и файловый storage проще для воспроизведения на ноутбуке с 16 GB RAM.
