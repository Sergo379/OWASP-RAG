# Этап №3 — фактические результаты

Дата: 27 октября 2026. Windows, Intel Core i5-1235U, 16 GB RAM, CPU; Ollama 0.40.11. Это техническая приёмка реализации, не evaluation качества и не выбор лучшей модели. Этапы №1–2 приняты пользователем. Этап №4 не выполнялся.

**Локальная генерация: IMPLEMENTED / TESTED. Langfuse SDK: IMPLEMENTED / TESTED. Langfuse Cloud: BLOCKED.**

**LANGFUSE CLOUD VERIFICATION: BLOCKED — USER CREDENTIALS REQUIRED**

## 1. Реализация

Работают существующий retrieval → context builder → versioned prompt → настоящий Ollama `/api/chat` → проверка citations → final. AI Chat поддерживает RU/EN, предварительный SSE поток, Stop/disconnect/timeout, ограниченную историю, новый диалог/clear, sources и diagnostics. Общий OS FileLock запрещает параллельные тяжёлые операции. Embedding/reranker освобождаются до LLM; keep_alive=0.

## 2. Файлы

Добавлены `src/generation/{config,context,ollama,resources,service}.py`, `src/api/stage3.py`, `src/observability/tracing.py`, `src/ui/stage3.js`, `src/stage3_cli.py`, `src/stage3_smoke.py`, `tests/test_stage3.py`, generation/prompts YAML, этот отчёт, MANUAL_TESTING_STAGE3.md и публичные JSON/screenshots в docs. Обновлены API/service/retrieval instrumentation/UI, log rotation, .env.example, Pixi/lock/requirements/version и README/ARCHITECTURE/ROADMAP/VALIDATION. Полный список соответствует diff коммита этапа №3.

## 3. Установленные модели

| Тег | Digest | Состояние |
|---|---|---|
| `qwen3:1.7b` | `8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7` | Ready, actual inference verified |
| `qwen3:4b` | `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7` | Ready, actual inference verified |
| `qwen3:4b-instruct-2507-q4_K_M` | `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0` | Ready, actual inference verified |

Обязательный `qwen3:4b` по реальным GGUF metadata — **Thinking-2507**. Он игнорирует отключение reasoning, что согласуется с [официальной карточкой Thinking-модели](https://huggingface.co/Qwen/Qwen3-4B-Thinking-2507). Для обычного 4B Chat дополнительно подготовлен [официальный Instruct Q4_K_M](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M). Его имя явно видно в UI/metrics; результаты не подменяются Thinking-тегом.

При диагностике также создан локальный `owasp-qwen3-4b:grounded-v1` (digest cbe1c773999cb7faa2c325dd4852976ebb1149008484cda93b2949a2e4b2de3f). Это только другой template на **тех же blobs исходного 4B**, что независимо проверено через FROM; дополнительных weights он не занимает и в итоговой конфигурации не используется. Исходные модели не удалялись/не перезаписывались.

## 4. Что действительно запускалось

В [stage3-llm-check.json](docs/stage3-llm-check.json) все три активных тега реально ответили READY последовательно на CPU. У Thinking `think=true`, structured thinking полностью отброшен; у 1.7B/Instruct — false. Наличие программы/файлов не выдаётся за inference.

**Известное ограничение:** обязательный Thinking-2507 на сложных/даже сфокусированных RAG запросах достигал num_predict. Последний сохранённый [реальный failed case](docs/stage3-thinking-limit.json): 2048 tokens budget, temperature 0.6, 345.62 s целиком; ответ не объявлен completed. Обычный Chat проверен на 1.7B и отдельном 4B Instruct. Для Thinking успешен короткий inference; успешный полный сложный RAG этим тегом **NOT TESTED / не получен в данном CPU budget**.

## 5–6. Реальные ответы, timings и RAM

[Полный real-model JSON](docs/stage3-real-results.json): один финальный последовательный offline-process прогон, **8/8 технических сценариев passed**. Параметры, вопросы, реальные тексты, source IDs, actual token usage и request/trace IDs сохранены. Это не 8/8 answer correctness или faithfulness.

| Сценарий | Status | Total s | TTFT s | Input / generated tokens | Peak Python+Ollama GiB |
|---|---|---:|---:|---:|---:|
| sql_en | completed | 41.49 | 18.16 | 1270 / 74 | 2.24 |
| password_ru | completed | 26.17 | 16.05 | 1075 / 97 | 2.25 |
| auth_en_rerank | completed | 46.52 | 17.49 | 1274 / 113 | 2.41 |
| weather_ru | refused | 4.30 | — | skipped | 0.65 |
| absent_fact | refused | 4.41 | — | skipped | 0.65 |
| injection | refused | 4.37 | — | skipped | 0.65 |
| qwen4_minilm | completed | 37.42 | 24.45 | 680 / 71 | 3.52 |
| partial | completed | 29.08 | 18.61 | 1298 / 97 | 2.31 |

4B строка — Instruct/MiniLM, Top-K=3 с явным фильтром SQL Injection Prevention; это сфокусированная проверка работы, не сопоставимый benchmark с 1.7B. Primary SQL example: parameterized queries / least privilege с настоящими citations. Russian example: получен русский ответ о salt/hash/work factor. Authentication/session example: получены CSPRNG/session/authentication рекомендации. Weather, private user count и instruction override дали refused; LLM для этих gates не запускался. Partial question дал общую часть и явное уведомление об отсутствии private credentials.

RAM сэмплировалась каждые 250 ms, включая **все Ollama процессы и дочерний llama-server.exe**, а не только Python. RSS сумма может повторно учитывать shared pages и пропустить короткие пики; это не расход физической памяти всей машины. До первого токена выполняются loading/prefill; TTFT не равен только decoding latency.

## 7. Citations

S1…Sn соответствуют только текущим выбранным chunks. Проверяются наличие/допустимость IDs, отсутствие сгенерированных URL и блоков без citations; source URL берётся из metadata. Источники сгруппированы по document_id; UI открывает section/chunk/text/scores. В browser реально открыт [S3] из SQL ответа: [screenshot](docs/stage3-chat-citations.png). **Корректный ID не доказывает entailment.**

## 8. Отказ и качество

Пустая выдача/threshold/актуальность источников, domain/entity/instruction heuristics, untrusted-document prompt, sentinel INSUFFICIENT_CONTEXT и citation validation — разные слои. При отсутствии доказательств выводится локализованный отказ. В частичном запросе app явно отмечает отсутствие private credentials; это не придуманная LLM цитата.

Содержательные ограничения сохранены честно: 1.7B может плохо формулировать русский текст и смешивать user-password storage с reset-token/application-secret advice из соседних chunks. В частности, random-password/secret-manager рекомендации требуют проверки роли секрета по открытым источникам. Citation validation не является semantic verifier. Формальная faithfulness/answer correctness, robust prompt-injection coverage и выбор лучшего retrieval/prompt — **NOT TESTED**, этап №4.

## 9. Streaming / Stop

SSE accepted/progress/token/final. До validation deltas предварительные. Stop/disconnect отменяет asyncio task и закрывает upstream HTTP даже до headers; частичный текст не считается completed. Реальный browser Stop: request `c4199197b6ba423b904fb26998d62928` → cancelled; после отмены `/api/ps` вернул пустой список. TCP tests отдельно подтвердили EOF у upstream при отмене/timeout до и после headers. История пережила restart и сохранила cancelled.

## 10–12. Langfuse

Современный **Langfuse 5.0.4 / OpenTelemetry**, не legacy trace()/span(). Default metadata-only; opt-in query/context/prompt/answer экспортируется с best-effort masking, без произвольных local files. Cloud export асинхронный, timeout 2 s; очередь SDK ограничена памятью, durable replay **NOT IMPLEMENTED**. Ошибка SDK init/observation/export не ломает core RAG. Slow failing exporter проверен через полный Chat pipeline.

Из финального реального прогона сохранены **104 настоящих SDK spans**, 8 roots OWASP_RAG_REQUEST, по 12 children с корректными parent IDs: INPUT_VALIDATION, QUERY_EMBEDDING, VECTOR_RETRIEVAL, SIMILARITY_FILTERING, METADATA_FILTERING, DEDUPLICATION, RERANKING, CONTEXT_BUILDING, PROMPT_CONSTRUCTION, OLLAMA_GENERATION, ANSWER_VALIDATION, FINAL_ANSWER. Embedding/generation имеют соответствующие типы, реальные IDs/scores/usage/timings. Reranking OFF и skipped generation помечены явно. На Windows SDK wall-clock stamps субмиллисекундных steps иногда совпадают; diagnostics используют perf_counter и сохраняют более точную длительность.

[Cloud status](docs/stage3-langfuse-status.json): ключей нет, реальный Cloud trace/UI **BLOCKED / NOT TESTED**. Локальная SDK hierarchy не выдаётся за Cloud PASS; ложный Open in Langfuse URL не показывается. Ключи нужно задать пользователю локально и повторить сценарий R.

## 13. Unit / integration

**171 passed, 1 warning** в финальном pytest. Старые 121 сохранены, 50 новых checks covering context budget/exclusions, history, citations/refusal/language, actual SDK hierarchy/privacy/failure, Ollama NDJSON/variant/digest/error/usage, API/SSE/origin, TCP cancellation/timeouts и совместную Windows log rotation. HTTP/LLM fixtures в pytest не выдаются за реальные LLM; они отделены от real-smoke.

## 14–15. Real models / regression

`pixi run lint` — passed. `pixi run smoke` — local passed. `pixi run real-smoke --offline --demos` — passed: 20 actual RU/EN retrieval queries, оба embeddings, нормализованные 384D vectors, фильтры и настоящий reranker. `pixi run rag-smoke --offline` — 8/8 технических случаев passed. `llm-check --inference` — все три реальные модели. [Actual connection-refused check](docs/stage3-ollama-unavailable.json) подтвердил безопасную ошибку без остановки пользовательского Ollama.

Offline означает запрет non-loopback sockets в Python-процессе. **Физическое отключение интернета в этапе №3 NOT TESTED**. Cloud недоступность не препятствовала локальной генерации.

[Data preservation](docs/stage3-data-preservation.json): проверены 57 raw SHA256, 55 active documents, 4 прежних published индекса и **10561 vectors** по catalog, 8 прежних JSONL files с сохранёнными SHA256. Этап №3 не строил/не очищал embeddings и не менял corpus/preprocessing/retrieval config. Operation history дополнена, не усечена.

## 16. Ручная проверка

[MANUAL_TESTING_STAGE3.md](MANUAL_TESTING_STAGE3.md) содержит 21 сценарий A–U с восемью обязательными пунктами для каждого. Browser QA выполнен в Chromium in-app browser: реальные ответы, citations, Stop, Ready/Cloud blocked, New/Clear и persistence после server restart. При проверке быстрого New→Send исправлена race: session transitions блокируют отправку/переключение, отправленный запрос удерживает собственный session ID. [Browser evidence](docs/stage3-browser-checks.json) подтверждает пустую новую history и Clear на созданном тестовом диалоге. Самостоятельная приёмка пользователя и отдельные Chrome/Edge новые UI checks — **NOT TESTED**. Cloud R требует собственных ключей.

## 17. Ограничения

Thinking-2507 может не закончить reasoning за CPU budget; fallback на другую модель не делается молча. Маленькие LLM дают содержательные ошибки даже при валидных IDs. Heuristics не являются полной защитой от injection. Masking не универсальный DLP. Chat clear очищает messages, не все request diagnostics на диске. Crash recovery помечает interrupted и не догенерирует автоматически. История session ограничена, долговременные локальные request records не удаляются автоматически.

## 18. Запуск

```powershell
Set-Location C:\MyWorkP\OWASP-RAG\lab1
pixi install --locked
pixi run llm-check --inference
pixi run app
```

Открыть [AI Chat](http://127.0.0.1:8000/#chat). Если сервер уже работает на 8000, второй процесс не запускайте. После изменения YAML/.env перезапустите сервер. Инструкция подготовки всех трёх тегов и Qwen tokenizer — README.

## 19. Git

Этап подготовлен для отдельного коммита в существующем main. Фактический SHA и подтверждённый результат push сообщаются в финальном ответе; этот файл не объявляет будущий push выполненным. Data/chat/models/weights/keys/logs и .pixi исключены из Git; tracked JSON содержит только фиксированные публичные demo questions и технические metadata.

## 20. Этап №4

**NOT IMPLEMENTED:** 40-question development/final protocol, systematic chunking/model/prompt comparison, Recall@K/MRR/Hit Rate, faithfulness/correctness evaluation, графики и исследовательские выводы. Демонстрационные вопросы этапа №3 не выдаются за такой benchmark.
