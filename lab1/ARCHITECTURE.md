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

## Контракты pipeline (retrieval/generation реализованы, evaluation — этап №4)

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

Search diagnostics responses хранятся только в памяти (10 min / 20 requests), локальное query logging по умолчанию отключено. При RAG тот же `execute_search` принимает внешний request_id и observer; вычисления обрамлены настоящими Langfuse observations, существующие Search API/defaults сохраняются.

Этап №3: `generation/service.Chat` берёт общий OS FileLock до worker start; retrieval → выгрузка ссылок на embedding/reranker → отбор целых оригинальных chunks по Qwen tokenizer → prompt → Ollama NDJSON → citation validation → final. Одновременно выполняется одна тяжёлая операция. `httpx.AsyncClient` работает внутри отдельного worker event loop; отмена task и общий `asyncio.timeout` закрывают upstream даже до первого токена/HTTP headers. `message.thinking` игнорируется, используется incremental tag filter. Исходный pinned qwen3:4b работает через think=true, так как его template игнорирует false: structured thinking отбрасывается целиком; fallback filter также скрывает inline prefix до закрывающего тега. Ни текст reasoning, ни поле thinking не сохраняются. Для Thinking-2507 output budget 2048 / temperature 0.6; для 1.7B и отдельной Instruct-2507 Q4_K_M — 512 / 0.1. Сложный Thinking RAG может достичь лимита и завершиться failed; Instruct — практичная дополнительная альтернатива для Chat. Partial answer при отмене/ошибке не получает статус completed.

`POST /api/chat/stream` возвращает SSE accepted/progress/token/final. Deltas предварительные. Disconnect отменяет запрос; переполненная ограниченная очередь дельт восстанавливает authoritative final по request ID. `POST /api/chat` + `GET /api/chat/requests/{id}` дают альтернативу без streaming. Sessions создаются сервером, IDs проверяются, история ограничена 40 сообщениями, prompt — 2 пары/400 tokens. Только успешные прежние пары могут попасть в prompt; старые citations удаляются. Новый session не читает чужую историю. В `data/chat` атомарно сохраняются локальные sessions/requests; orphan running помечается interrupted при проверке свободного OS lock, частичный checkpoint восстанавливается. Это не durable task queue.

Контекст задаёт S1…Sn только текущим выбранным chunks; source URL приходит из metadata, не из LLM. Группировка — по document_id. Heading-only, подозрительные инструкции и chunks вне токен-бюджета исключаются с причиной. System prompt считает документы недоверенными, запрещает внешние знания и требует INSUFFICIENT_CONTEXT при отсутствии факта. Дополнительные gates проверяют явный off-domain, instruction override, отсутствие именованного частного объекта; validator проверяет существование/наличие citation IDs, язык, URL и блоки без ссылок. Эти проверки не доказывают entailment: human review нужен даже при корректных IDs.

`observability/tracing.py` использует Langfuse 5.0.4 и настоящий OTel exporter. Root OWASP_RAG_REQUEST и 12 children создаются непосредственно перед этапами; ошибка заканчивает открытые spans с error/interrupted. Generation имеет тип generation и фактический token usage. Метаданные включают identity/snapshot, IDs/scores, исключения, параметры и timings. При отключённом reranking span содержит skipped. Python/Ollama RSS сэмплируются каждые 250 ms; сумма RSS может повторно учитывать shared pages.

Без ключей экспортёр локальный bounded in-memory; Cloud verification BLOCKED. С ключами SDK batch processor асинхронно отправляет OTLP с timeout 2 s; auth/link проверяется отдельно в фоне. Core request не делает Cloud auth/flush/URL lookup. Export failure отображается в status; durable replay отсутствует. Metadata-only — default; экспорт query/context/prompt/answer — opt-in с ограниченным masking. Локальные chat records содержат пользовательские сообщения, исключены из Git и не отправляются в Cloud по умолчанию. Технические логи не содержат credentials или thinking.

Компоненты можно позднее выделить в сервисы лабораторной №2, сохранив API-границы application layer. Сейчас один процесс и файловый storage проще для воспроизведения на ноутбуке с 16 GB RAM.


## Stage 3 Quality Fix: явный Chat pipeline

Baseline `retrieval/pipeline.py` сохраняет порядок и scores Search, дополнительно возвращая summaries полного candidate pool. Chat `hybrid_context` использует этот pool и BM25 по оригинальным chunks опубликованного Chroma generation (read-only snapshot, cache одного index/generation). Применяются active/raw-SHA/topic/document/threshold ограничения. RRF 60 объединяет списки; title affinity, requested-term novelty и named-control ограничения выбирают контекст. Это настраиваемая стратегия Chat; model/index identity не подменяются. Optional cross-encoder применяется один раз к объединённому pool, его scores определяют порядок. Отдельные spans: LEXICAL_RETRIEVAL, HYBRID_FUSION, CHAT_RERANKING, CONTEXT_CANDIDATE_SELECTION.

Context builder учитывает полный prompt и профиль режима, целиком исключает неумещающиеся chunks. Только в передаваемом prompt Markdown HTTP destinations убираются с сохранением label; transformation записана, raw originals не изменяются. Диагностика различает semantic/fused pools, selected context и cited sources; UI показывает переданный текст и оригинал. Evidence — буквальные offsets/quotes и lexical overlap, не entailment.

Валидация расширена source/request binding, известной Secure-cookie ошибкой, literals и quotations. На конкретных исправимых ошибках возможна одна локальная генерация ANSWER_REPAIR с ANSWER_REVALIDATION; остальная часть pipeline не повторяется. Budget времени ограничен исходным timeout, prompt проверяется повторно, Stop действует через тот же cancellation. Первичная ошибка и оба usage/timing сохраняются локально, rejected text/evidence quotes не добавляются в metadata Cloud. Valid citations не гарантируют правильность всех причинных/технических выводов.

`generation/evidence.py` реализует независимый опциональный формат `evidence_bound`. Runtime-маркеры S1P2 привязаны к char ranges неизменённого исходного chunk; заголовки/слова не удаляются. JSON Schema Ollama разрешает для каждого источника только его paragraph IDs или UNUSED. Русское/английское поле и один слот на источник уменьшают копирование английского текста и циклические повторы. Модель формулирует текст, приложение добавляет citations и отображает исходный evidence; генерируемых моделью цитат нет. Синхронный incremental decoder выдаёт готовые блоки в существующий SSE token event. Сырой JSON и incomplete slots в UI не попадают. Stop закрывает тот же upstream HTTP.

Для evidence_bound при включённом repair резервируются 384 input tokens под компактный feedback; код повторно проверяет фактический prompt и оставшееся время. Feedback указывает конкретный source/paragraph и неподдержанные значения, а не только общий failure label. `evidence_issues`, как и evidence quotes, исключаются из SDK metadata. Проверка языка допускает технические имена в начале русского предложения (`REST API`, `TLS`); она не является оценкой качества перевода.

После decode проверяются schema errors, принадлежность выбранного абзаца текущему источнику/request, char ranges ответа, source IDs и literals/короткие технические identifiers по выбранному абзацу с его заголовком. Evidence quotes остаются только в локальном record/ответе API; SDK metadata содержит IDs/offsets/errors/usage. Полная schema и paragraph markers учитываются в prompt budget, неуместившиеся chunks исключаются целиком с причиной. `ttft_ms` в этом формате измеряет первый отображённый блок; `raw_ttft_ms` отдельно измеряет первый токен модели. Eval usage включает JSON framing. Legacy API default — prose; UI/CLI явно выбирают формат. Concise и legacy Thinking по умолчанию сохраняют prose. Schema связывает структуру и provenance, но не доказывает entailment/полноту/правильность перевода.

## Stage 4 · Evaluation

`src/evaluation` читает замороженный dataset с SHA-256, проверяет его относительно активных raw blobs и планирует измерения. Для поиска использует `execute_search`, для генерации — новый сеанс стандартного `Chat` на вопрос. Evaluation использует те же модели, tokenizer, preprocessing, ChromaDB, валидацию источников и Ollama. Рабочий индекс переиспользуется при совпадении snapshot, configuration ID, веса и политики индексации. Новый индекс строится в отдельном workspace с локальной копией вычислительного кеша. Общий operation lock предотвращает параллельную загрузку тяжёлых моделей.

`RunStore` сохраняет неизменяемый manifest, атомарные checkpoints попыток и отдельное состояние UI. Stop — файл запроса на остановку, который проверяется между операциями и на границе batch. Для генерации worker вызывает существующую отмену Chat. Resume пропускает завершённые строки, сверяет snapshot, pipeline и index generation. Повтор ошибки добавляет попытку, сохраняя предыдущую. Результаты можно выгрузить в JSON/CSV и SVG.

Смысловые оценки хранятся отдельно от автоматической проверки citation IDs и отказов. Запись привязана к request ID и попытке. Report и Dashboard показывают знаменатели, ошибки, параметры и trace IDs. Страница Research / Evaluation отправляет запуск отдельному локальному Python worker с тем же runner, что у Pixi CLI.
