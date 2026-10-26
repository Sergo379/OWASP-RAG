# OWASP-RAG · этапы №1–2

Цель — собственная воспроизводимая RAG-система по безопасности веб-приложений. Требования: [задание преподавателя](https://github.com/Randwow/PAD). Первый этап принят пользователем. Сейчас доступны Foundation, Grabber, Preprocessing, Dashboard, Knowledge Base и настоящий семантический поиск RU/EN с ChromaDB и reranking. Генерации ответов LLM пока нет.

## Обоснование решений

- **OWASP**: тематически связанная техническая документация с явными рекомендациями, примерами кода и открытой лицензией. 55 выбранных материалов охватывают Authentication, Authorization, Session Management, Injection, XSS, API Security, Cryptography, Input Validation, Logging/Monitoring и Secure Configuration.
- **Python 3.11**: зрелые библиотеки HTTP/API, типизированные модели и последующая интеграция с NLP без отдельной платформы.
- **Pixi**: отдельная среда проекта, lockfile с точными версиями, единые команды PowerShell. `requirements.txt` — справочное отражение ограничений `pixi.toml`; устанавливать через pip отдельно не нужно.
- **Markdown из GitHub**: структура заголовков и code blocks, точные blob SHA и commit. Не требуется скачивать историю Git или парсить навигацию сайта.
- **FastAPI + обычные HTML/CSS/JS**: один сервер, без Electron, Node dev-server, Docker и фоновой ML-инфраструктуры.
- **Файлы + атомарные указатели**: легко показать raw, manifest и JSONL на защите; интерфейс storage можно расширить отдельным векторным хранилищем.

## Установка и запуск

Windows 11 x64, CPU, 16 GB RAM. Pixi 0.81.0 или совместимая версия. Системный Python не используется. Интернет нужен для установки, обновления OWASP и явной подготовки моделей. На этапе №2 полные safetensors трёх моделей занимают вместе около 1.46 GB с токенизаторами. Готовый поиск offline использует только локальные файлы.

```powershell
Set-Location C:\MyWorkP\OWASP-RAG\lab1
pixi install --locked
pixi run smoke
pixi run app
```

UI: [http://127.0.0.1:8000](http://127.0.0.1:8000). Остановка сервера: `Ctrl+C`. Запуск только на loopback, один worker. Работает независимо от Codex и VS Code. Для проверки в браузере используйте Chrome или Edge.

Из второго PowerShell, также из `lab1`:

```powershell
pixi run grab
pixi run preprocess
pixi run stats
pixi run test
pixi run lint
pixi run smoke --network
```

`smoke` проверяет локальные endpoints и загрузку HTML, не обращается к сети. `--network` дополнительно выполняет настоящий discovery и проверяет все 55 путей; при недоступной сети возвращает код 1, а не фиктивный успех. Команды `grab`/`preprocess` возвращают код 1 при ошибке или частичном результате.

## Конфигурация

| Файл / переменная | Назначение |
| --- | --- |
| `configs/corpus.yaml` | Репозиторий, ref, 55 путей с тематикой, timeout, retries, лимит размера |
| `configs/preprocessing.yaml` | Стратегия, размер, overlap, версия pipeline и закреплённый токенизатор |
| `configs/logging.yaml` | Уровень и ротация локальных JSON-логов |
| `configs/retrieval.yaml` | Pinned models, batch size, CPU threads, token budgets, политики индексации, query logging |
| `configs/model_artifacts.json` | Размер и SHA256 каждого разрешённого файла weights/tokenizer/config |
| `GITHUB_TOKEN` | Необязательный токен только в окружении; отправляется только GitHub API |
| `OWASP_DATA_DIR` | Необязательный альтернативный каталог данных |

После изменения YAML перезапустите сервер. `.env.example` служит памяткой; автоматической загрузки `.env` нет. Устанавливайте необходимые переменные в PowerShell до запуска. Для обычного публичного корпуса токен не требуется: discovery использует два API-запроса; файлы скачиваются с raw.githubusercontent.com. Не записывайте токен в команды, публикуемые на защите, или в Git.

```powershell
# Отдельный пустой корпус для проверки, без изменения основного:
$env:OWASP_DATA_DIR = 'C:\MyWorkP\OWASP-RAG\lab1\data\manual-empty'
pixi run app
# После остановки:
Remove-Item Env:OWASP_DATA_DIR
```

## Grabber и change detection

`commits/{ref}` фиксирует commit, затем recursive Git tree обнаруживает файлы. Все raw URL содержат этот commit; смешивания разных версий в одном запуске нет. `ref` можно заменить на сохранённый commit для воспроизведения snapshot.

`document_id = SHA256(repository + ':' + source_path)` стабилен при изменении текста. Blob SHA сравнивается с manifest. Для неизменённого документа проверяется целостность локального файла, после чего загрузка пропускается. Новые/изменённые bytes сверяются с Git blob SHA и сохраняются под SHA256 содержимого. Повреждённый локальный raw загружается заново.

Manifest содержит `document_id`, `source`, `source_path`, официальный опубликованный `url`, `title`, `topic`, `git_blob_sha`, `content_sha256`, `repository_commit`, `fetched_at`, `updated_at`, `last_checked_at`, `status`. `updated_at=null`: достоверная дата последнего редактирования не запрашивается. `repository_commit` документа — provenance его реально загруженных bytes; верхний commit manifest — последний проверенный snapshot.

Файлы immutable; manifest обновляется атомарно после каждого документа. Прерванный запуск можно повторить. Старые raw сохраняются. Удалённые в источнике документы получают `deleted`, исключённые из corpus.yaml — `out_of_scope`; пользовательские данные автоматически не удаляются. Поэтому число исторически скачанных документов может превышать число активных (например, при замене устаревшего материала). Два обнаруженных DEPRECATED-материала заменены в итоговом списке на Authorization Patterns и Post Quantum Cryptography.

HTTP-запросы выполняются последовательно. Timeout и число повторов ограничены. Для временных сетевых ошибок/5xx используется exponential backoff. Для 403 rate limit и 429 учитываются серверные заголовки; длинное ожидание завершается понятной ошибкой «повторите позже». 404 не повторяется. Прерывание загрузки не публикует непроверенные bytes.

## Preprocessing и токены

Собственная обработка удаляет YAML front matter и HTML-комментарии вне code blocks, убирает изображения с сохранением alt-текста, нормализует переносы и лишние пустые строки. Заголовки ATX превращаются в `heading_path`; списки, ссылки, inline code, SQL, HTTP-заголовки и содержимое code blocks сохраняются. Технический HTML намеренно не удаляется агрессивно. Внешние документы никогда не выполняются как код/инструкции. В UI они выводятся через `textContent`.

| CLI-стратегия | Поведение |
| --- | --- |
| `fixed` | Последовательные окна до token budget без overlap |
| `overlap` | Окна с перекрытием, `floor(size × overlap)` токенов; на границах subword реальный overlap может немного отличаться |
| `paragraph` | Упаковка целых абзацев внутри разделов; длинные абзацы разбиваются ограниченными окнами |
| `section` | Каждый раздел отдельно; длинные разделы разбиваются ограниченными окнами |

Размер — верхний предел, последний chunk может быть короче. Размеры 128/256/384, overlap 0/10/20%. Ненулевой overlap принимается только стратегией `overlap`. Пустых chunks нет. Длинные code blocks могут быть разделены между chunks, но их содержимое не отбрасывается.

```powershell
pixi run preprocess --strategy fixed --chunk-size 128
pixi run preprocess --strategy overlap --chunk-size 256 --overlap 0.1
pixi run preprocess --strategy paragraph --chunk-size 384
pixi run preprocess --strategy section --chunk-size 256
```

Используется настоящий Rust-backed токенизатор `intfloat/multilingual-e5-small` через библиотеку `tokenizers`. Revision и SHA256 закреплены в YAML. Только `tokenizer.json`, без transformers/torch/embedding-модели. `pixi run tokenizer` позволяет заранее подготовить кеш. Offline preprocessing после этого не требует интернета. Усечение и padding выключены; длина повторно проверяется на **реальном тексте каждого сохранённого chunk**. Максимум 384 + резерв 32 < 512. На этапе №2 необходимо повторно проверить полный ввод с `passage:` и специальными токенами; для второго embedding-токенизатора нельзя считать E5 count универсальным.

Каждый chunk содержит все обязательные поля: `chunk_id`, `document_id`, `title`, `source_path`, `url`, `section`, `heading_path`, `chunk_index`, `chunking_strategy`, `chunk_size`, `overlap`, `content_sha256`, `source_commit`, `token_count`. Дополнительно: `text`, `tokenizer`, `configuration_id`. Здесь `content_sha256` — SHA256 текста chunk; SHA исходного документа записан в manifest и run summary. Индексы chunks начинаются с 0. ID зависит от документа, версии raw, конфигурации, позиции и текста. Одинаковые snapshot и конфигурация дают одинаковые chunks/ID.

`data/processed/<configuration_id>-<output_hash>.jsonl` хранит результаты; `index.json` содержит полную конфигурацию, source snapshot и счётчики. Стратегии не перезаписывают друг друга. Повторный запуск той же конфигурации обновляет её указатель, а старый JSONL остаётся. Dashboard показывает последнюю завершённую конфигурацию; просмотрщик позволяет выбрать другую. Изменённые/неактивные документы не учитываются в актуальных processed-счётчиках, старые chunks отмечаются stale.

## API и состояние операций

UI → FastAPI → `core/service.py` → Grabber/Preprocessing → `storage/files.py`. CLI вызывает тот же Service. Межпроцессный FileLock блокирует конкурирующие мутации, включая UI + CLI. История последних 50 операций сохраняется на диске. После аварии освобождённый OS lock позволяет отметить старую операцию interrupted и повторить её.

| Метод и путь | Результат |
| --- | --- |
| `GET /api/health` | Статус процесса и версия |
| `GET /api/state` | Компоненты и текущая операция |
| `GET /api/stats` | Реальные счётчики и доступные конфигурации |
| `GET /api/documents` | Список, metadata, количество chunks |
| `GET /api/documents/{id}` | Metadata + raw Markdown |
| `GET /api/documents/{id}/chunks` | `configuration_id`, `offset`, `limit` (до 100), total/stale |
| `POST /api/operations/grab` | Запуск, HTTP 202 |
| `POST /api/operations/preprocess` | JSON с strategy/chunk_size/overlap, HTTP 202 |
| `GET /api/operations/current` | Текущее/последнее состояние |
| `GET /api/operations/{id}` | Состояние из текущей операции или истории |
| `GET /api/errors` | Последние ошибки |

OpenAPI: `/openapi.json`, Swagger UI: `/docs` (Swagger assets могут требовать интернет). Ошибки: 400 — прикладная ошибка, 404 — ресурс отсутствует, 409 — конфликт операций, 422 — некорректный JSON/параметры, 500 — безопасное сообщение внутренней ошибки. Принятие фонового запроса (202) не означает успех работы: проверяйте статус `succeeded`, `partial`, `failed` или `interrupted`.

## Файлы и тесты

`src/core`, `grabber`, `preprocessing`, `storage`, `observability`, `api`, `ui` — действующие модули первого этапа. Этап №2 добавляет `embeddings`, `retrieval`, `storage/vectors.py`, `api/stage2.py`, `ui/stage2.js` и CLI/smoke. Filtering, deduplication и reranking входят в собственный retrieval pipeline. Generation/evaluation — будущие контракты в [ARCHITECTURE.md](ARCHITECTURE.md), без фиктивных функций.

```powershell
pixi run test
pixi run lint
pixi run smoke
pixi run smoke --network
```

Unit и integration tests используют fixtures и mocked HTTP; внешняя сеть запрещена тестовой фикстурой (локальный socketpair Windows asyncio разрешён). Покрыты первый/повторный/изменённый сбор, stable IDs, удаление, повреждение raw/manifest/JSON, 404/429/rate limit/timeout/retry, прерывание/продолжение, атомарность, очистка технических документов, токены/размеры/overlap/детерминизм, все стратегии, API, ошибки, пустой корпус, конфликты и перезапуск. [Ручные проверки](MANUAL_TESTING.md), [фактические измерения](VALIDATION.md).

JSON-логи: `lab1/logs/application.log`, ротация 2 MB × 4 файла. Поля: timestamp, level, module, operation, message, error_details. Неожиданные ошибки содержат тип и позиции вызовов без секретов, exception messages и локальных переменных. Время в файлах — UTC, UI показывает локальное время браузера. Заголовки Authorization не логируются.

Резервная копия — копия каталога `data` при остановленных операциях. Для воспроизведения другого snapshot используйте отдельный `OWASP_DATA_DIR` и сохранённый commit в corpus.yaml. Память на этапе №1 ограничена одним небольшим корпусом и одним токенизатором; embeddings/reranker/LLM не загружаются. Сохранение JSONL сейчас пакетное; для очень больших корпусов потребуется потоковая запись.

## Семантический поиск (этап №2)

Последовательность: **Prepare Model → Build Index → Search**. Все операции доступны в Knowledge Base; Search встроен в существующий sidebar.

```powershell
pixi run prepare-model --model e5
pixi run prepare-model --model minilm
pixi run prepare-model --model reranker
pixi run build-index --model e5 --configuration-id 95c6ca90edb66fce3ee9
pixi run build-index --model minilm --configuration-id 95c6ca90edb66fce3ee9
pixi run indexes
pixi run inspect-index --index-id <ID>
pixi run search --model e5 --index-id <ID> --query 'Как предотвратить SQL-инъекции?' --top-k 5
pixi run search --model e5 --index-id <ID> --query 'How to prevent SQL injection?' --reranking
pixi run real-smoke --offline --demos
```

В PowerShell замените `<ID>` полным ID из списка индексов (угловые скобки не вводите). Команды подготовки/индексирования/поиска выполняются по одной. Существующие команды сохранены. `real-smoke` отдельно от pytest действительно загружает локальные pretrained weights и обращается к Chroma; он не скачивает недостающие модели автоматически.

| Key | Model / revision | Назначение / input limit |
| --- | --- | --- |
| e5 | `intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3` | 384D, 512 токенов; query:/passage: |
| minilm | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2@e8f8c211226b894fcb81acc59f3b34ba3efd5f42` | 384D, **128** токенов; без E5-prefix |
| reranker | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1@1427fd652930e4ba29e8149678df786c240d8825` | 512 токенов на пару; raw logit |

PyTorch **2.8.0+cpu** установлен из официального CPU wheel index, Transformers **4.57.6**, ChromaDB **1.5.9**. Точные transitive dependencies закреплены Pixi. Sentence-transformers/LangChain не обязательны: собственный provider выполняет attention-mask mean pooling по официальным model cards и L2 normalization. Только safetensors, `trust_remote_code=False`, локальные CPU weights; вопрос не отправляется во внешний API.

Prepare — явная сетевая операция с timeout, bounded retries, фактическим byte progress и проверкой всех SHA256. Inference использует `local_files_only=True`. Lazy cache сохраняет одну embedding-модель; при переключении старая выгружается. Reranker — отдельная необязательная модель, выгружается при поиске без reranking. Небольшие batches=8, threads=4. Удаление Python-ссылок не гарантирует немедленное снижение RSS allocator: полный сброс — restart сервера при сохранённых дисковых данных.

Каждый полный input проверяется собственным tokenizer модели, включая prefixes/special tokens. `overflow_policy=split` создаёт отдельные vector segments с origin chunk_id и char ranges, не меняя исходный JSONL. `error` останавливает Build с диагностикой. Минимальный анализ показывает распределение tokens, короткие chunks и heading-only. `include_all` — default; `skip_heading_only` исключает только целиком заголовочные фрагменты с сохранением причины. Автоматического объединения нет.

Индекс идентифицируется source snapshot fingerprint/commit, preprocessing config + JSONL, model/revision/artifacts, embedding policy и schema version. Каждый logical index имеет собственный PersistentClient directory в `data/vector_db/<index_id>/chroma`. Collection generation строится staging, проверяется count и набор IDs; только затем атомарно публикуется в catalog.json. Повторный Build Ready возвращает reused без inference. Resume использует сохранённые batches и SQLite computation cache; Rebuild пишет новое поколение, старое сохраняется. Кеш не заменяет ChromaDB. Размер каталога включает все сохранённые поколения. Физическое повреждение Chroma требует backup/rebuild; транзакции между SDK и JSON catalog не обещаются.

Retrieval: validate → query embedding → Chroma nearest neighbors → threshold/stale/metadata filters → deterministic dedup by origin chunk/text → optional cross-encoder → final Top-K. Metadata pushdown выбирает candidate pool среди подходящих источников; проверки metadata повторяются после retrieval. Top-K=3/5/10/20, pool default=80, не менее 2K, до 500. Пул и итог K — разные значения. Если кандидатов недостаточно после filtering/dedup, показывается фактическое число.

Метрика — **cosine distance**, similarity = **1 − distance**, диапазон similarity −1..1. Threshold применяется к similarity. Cross-encoder score — отдельный **raw logit**, не вероятность. Reranker получает тот же eligible pool; сравнение without/with не выполняет другой retrieval. Для длинных текстов — явные окна до 320 tokens, query до 128, проверка полного pair budget и max raw logit по окнам; transformation сохраняется в metadata. Улучшение качества из изменения порядка не следует.

Search показывает тексты/источники, original/final ranks, similarity/distance/logit, metadata, before/after и Search Execution Details с request_id, snapshot, settings, removed reasons, timings и RSS. Stale index отмечается; изменённые/удалённые документы исключаются. Все мутации и inference сериализованы тем же FileLock, поэтому при занятом CPU/storage запрос получает понятный 409; фоновой очереди поиска нет.

Новые API (старые сохранены):

| Метод / путь | Назначение |
| --- | --- |
| GET `/api/embeddings/models` | missing/downloading/ready/error, cache size, memory status |
| POST `/api/embeddings/prepare` | JSON `{ "model": "e5" }`, 202 |
| GET `/api/indexes` | каталог, Ready/Building/Stale/Failed/Interrupted |
| POST `/api/indexes/build` | model, configuration_id, heading_policy, overflow_policy, rebuild; 202 |
| GET `/api/indexes/{id}` | actual count, identity, analysis/excluded reasons |
| POST `/api/indexes/{id}/rebuild` | новое поколение для актуального snapshot; 202 |
| GET `/api/chunks/analysis` | optional configuration_id, анализ коротких chunks |
| GET `/api/retrieval/configuration` | preprocessing configs, topics, documents, defaults |
| POST `/api/search` | SearchRequest; настоящий синхронный SearchResponse |
| GET `/api/search/{request_id}` | diagnostics в памяти до 10 минут, последние 20 запросов |

Search JSON: `query`, `index_id`, `model`, `top_k`, `similarity_threshold`, `topic`, `document_id`, `reranking`, `candidate_pool_size`. Схемы Pydantic доступны в OpenAPI. 422 — validation, 409 — занято, 404 — неизвестный ресурс GET, 400 — прикладная ошибка с действием восстановления. Model/index failures не превращаются в фиктивный успешный ответ.

Локальные JSON-логи сохранены, в PowerShell добавлена последовательность QUERY/EMBEDDING/RETRIEVAL/FILTER/DEDUP/RERANK/RESULT с request_id, настоящими IDs/scores/timings. Полный query не пишется в постоянный лог без `log_queries:true`; диагностика ответа остаётся доступна в памяти. Ключи/токены не логируются. Операционная история первого этапа сохраняется без дальнейшего усечения.

Тесты разделены: обычный pytest — offline unit + real-Chroma integration с mock/tiny models; `real-smoke` — настоящий E2E по подготовленным weights/индексам; browser QA — отдельно. [Ручная приёмка A–R](MANUAL_TESTING_STAGE2.md), [реальные результаты](STAGE2_RESULTS.md), [сохранённые demo queries](configs/demo_queries.json). Преподаватель требует embeddings/vector DB/retriever/filter/reranker: реализовано на этапе №2; генерация/Langfuse и исследовательская оценка остаются в этапах №3–4.

## Ограничения и дальнейшая работа

Ollama, AI Chat/LLM generation, Langfuse Cloud и полноценная evaluation не реализованы. UI честно показывает этапы №3–4. Нет научных выводов об оптимальной модели/стратегии. Десять демонстрационных запросов и latency/RAM smoke подтверждают работоспособность, а не итоговое качество RAG. Необязательное удаление индексов через UI не реализовано; данные автоматически не удаляются. Подробнее: [ROADMAP.md](ROADMAP.md).

Данные: [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/), авторы OWASP Foundation и участники. [CC BY-SA 4.0](https://github.com/OWASP/CheatSheetSeries/blob/master/LICENSE.md). При распространении raw/chunks сохраняйте атрибуцию, ссылку на источник, указание преобразований и ту же лицензию. Все изменения pipeline — подготовка/нормализация/chunking. Это самостоятельный учебный проект, не официальный продукт OWASP.
