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

## Контракты pipeline (retrieval реализован, generation/evaluation — следующие этапы)

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

Схемы Document/Chunk в `src/core/models.py` сохранены. Этап №2 использует `embeddings/provider.py` (CPU mean pooling/L2), `storage/vectors.py` (Chroma + поколения + вычислительный кеш), `retrieval/pipeline.py` (filters/dedup/reranking/diagnostics), `api/stage2.py` и `ui/stage2.js`. Pydantic request/response models — `retrieval/models.py`.

Полный input считается собственным tokenizer: E5 512 с query:/passage:, MiniLM 128 без этих prefixes. Oversized chunks получают детерминированные segments с char ranges, origin chunk ID, model_input_tokens и transformation; оригинальный JSONL не меняется. Для cross-encoder проверяется pair budget, query≤128, passage windows≤320, raw logits агрегируются max. Это явная политика, а не тихое усечение.

Logical index ID = SHA256(source commit + активные document IDs/raw SHA/topics + preprocessing identity/file + model/revision/artifacts + embedding config + schema)[:32]. Каждый index имеет собственный Chroma directory и поколения collections. IDs векторов детерминированы logical identity/origin chunk/segment/text SHA. SQLite embedding cache хранит только повторно используемые вычисления по model/artifacts/input; поисковые операции идут в Chroma.

Build создаёт staging generation, сохраняет подтверждённые batches, проверяет точный набор IDs/count, пишет sidecar оригинальных chunks/исключений и только затем публикует атомарный catalog pointer. Failed/Interrupted не отменяет прежний published generation. Resume и Rebuild различаются: Resume продолжает attempt, Rebuild создаёт следующее поколение. Физические Chroma failures не прикрываются обещанием распределённой транзакции: нужны backup и rebuild. Размер index directory включает поколения.

Один OS operation lock сериализует все mutations и inference; поиск при занятых ресурсах возвращает 409. ModelManager держит одну embedding-модель; reranker — отдельный optional slot. Для обычных последовательных запросов weights не перечитываются. При переключении ссылки очищаются/GC вызывается, но allocator и native Chroma могут удерживать RSS до restart.

Search принимает pool≥2K (default80, max500), применяет metadata pushdown и затем проверяет threshold, metadata, актуальность raw SHA/status. Dedup исключает одинаковый origin chunk и одинаковый исходный text SHA. Cosine similarity=1−distance; raw rerank logit не нормализуется в вероятность. Before/after используют один eligible candidate pool. Old snapshot отображается Stale; изменённые/удалённые источники не попадают в актуальную выдачу.

Диагностические события являются точкой расширения для Langfuse: request_id, stage, timing, model/index identity, candidate IDs/scores, removed reasons. JSON и console sinks действуют сейчас; query не сохраняется постоянно по умолчанию. Полные diagnostics responses хранятся только в памяти (10 min / 20 requests). Ни Langfuse imitation, ни LLM generation на этапе №2 нет.

На этапе №3 Langfuse Cloud обязателен: query → embedding → retrieval → filtering → reranking → context → generation → validation → final answer. Для каждого span — inputs/outputs, параметры, timing/errors; отдельно candidate IDs, scores до/после reranking, Top-K, snapshot, LLM и prompt version. Проверить актуальный официальный SDK перед реализацией. Ошибка observability не должна ломать основной RAG.

Компоненты можно позднее выделить в сервисы лабораторной №2, сохранив API-границы application layer. Сейчас один процесс и файловый storage проще для воспроизведения на ноутбуке с 16 GB RAM.
