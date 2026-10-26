# Этап №2 — фактические результаты

Проверки: **27 октября 2026**, Europe/Bucharest. Базовый код этапа №1: `3870d16`. Первый этап принят пользователем. Это техническая проверка retrieval, не научная оценка качества и не окончательная пользовательская приёмка этапа №2.

## Среда и сохранённые данные

Windows x64, **Intel Core i5-1235U, 10 cores / 12 threads**, 16 GB RAM (ОС сообщает 16 869 548 032 bytes). Python 3.11, Pixi 0.81.0. CPU inference: batch=8, torch threads=4. CUDA, Docker, внешние inference API не используются.

Проверены и закреплены: PyTorch **2.8.0+cpu**, Transformers **4.57.6**, ChromaDB **1.5.9**, NumPy **2.4.6**, tokenizers **0.22.2**, FastAPI **0.162.0**; все transitive dependencies — `pixi.lock`. CPU torch установлен из официального `download.pytorch.org/whl/cpu`. Зависимости устанавливались только Pixi.

Исходное локальное состояние проверено до изменений: Git clean, `main`, 57 документов / 55 active / 2 out_of_scope, latest preprocessing=paragraph / 256 / 0%, 2 323 chunks. Manifest уже проверен на source commit **`9d41c054686e35fb56070b4b6271ae2829edea6a`**, тогда как сохранённые JSONL созданы на предыдущем snapshot. Их raw SHA совпадают с актуальными документами: JSONL не переписывались ради индексации, source lineage сохранён. Source snapshot каждого нового индекса фиксирует текущий commit и карту raw SHA/topics.

Raw, manifest, старые JSONL, конфигурации и история не удалялись. Повторный реальный preprocessing paragraph подтвердил 55 документов / **2 323 chunks** и тот же output filename/hash `9488c1f4e057ae271e0b-272aa7b1881c31fa23be.jsonl`. История операций далее сохраняется без усечения. Сетевая smoke подтвердила существование всех 55 выбранных путей без обновления корпуса.

## Настоящие модели

| Key | Repository / revision | Размер выбранных файлов | Подготовка: download / CPU load+inference |
| --- | --- | ---: | ---: |
| e5 | intfloat/multilingual-e5-small · `614241f622f53c4eeff9890bdc4f31cfecc418b3` | 487 725 595 B | 57.34 s / 10.97 s |
| minilm | sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 · `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` | 479 724 528 B | 43.49 s / 5.76 s |
| reranker | cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 · `1427fd652930e4ba29e8149678df786c240d8825` | 487 676 923 B | 42.44 s / 6.00 s |

Все три модели скачаны из официальных публичных snapshots; пять файлов каждой проверены SHA256 по `configs/model_artifacts.json`. Загружались только config/tokenizer/safetensors, без Python-кода модели, ONNX/OpenVINO/TF-дубликатов. В каждой Prepare выполнен настоящий CPU inference, `inference_verified:true`. Лицензии: E5 MIT; MiniLM/cross-encoder Apache-2.0.

E5: mean pooling + L2 normalization, query:/passage:, 384D, полный input≤512. MiniLM: тот же официальный mean pooling + L2, **без E5-prefix**, собственный tokenizer, рабочий limit **128**. Cross-encoder: оригинальные raw logits (Identity activation), 512 tokens pair budget, явные passage windows≤320, query≤128, max logit по окнам. Исходные chunks не усекаются.

## Построенные persistent индексы

Source commit всех индексов: `9d41c054686e35fb56070b4b6271ae2829edea6a`. Размерность 384. Metric=cosine. Все коллекции настоящие ChromaDB PersistentClient; локальный SQLite cache служит только повторному использованию вычислений.

| Index ID | Модель / config / policy | Origin chunks | Vectors | Первая сборка | Вычислено / cache hits |
| --- | --- | ---: | ---: | ---: | ---: |
| `b4f35294b84e0f83fda0e36cd6fed34b` | e5 / section 256 / include_all | 2 159 | 2 159 | 240.38 s | 2 156 / 3 |
| `1211ddafcaf5400734f679698eeee7ef` | minilm / section 256 / include_all | 2 159 | 4 055 | 224.78 s | 4 038 / 17 |
| `5dc3f3a6956f5420f4bb5d5ebb03efbb` | e5 / paragraph 256 / include_all | 2 323 | 2 323 | 139.99 s | 1 184 / 1 139 |
| `25336b5a0e0e333ac6ba7248fdbdf0cb` | e5 / section 256 / skip_heading_only | 2 159 | 2 024 | 22.95 s | 0 / 2 024 |

В каждом индексе — 55 документов. MiniLM создаёт несколько segments для длинного origin chunk: char ranges, origin ID и transformation доступны в metadata. Число его векторов не является числом исходных chunks.

E5 section Rebuild реально выполнен: опубликовано generation **2**, 2 159 vectors, **0 новых embeddings**, 2 159 cache hits, **27.70 s**. Предыдущее поколение сохранено. Повторный обычный Build после этого: `reused:true`, `computed_vectors:0`, `reused_vectors:2159`, **0.76 s**. Новых дубликатов нет.

Размеры каталогов на момент проверки (включая сохранённые поколения и sidecar, без общего computation cache): E5 section **55 336 216 B**, MiniLM section **41 100 704 B**, E5 paragraph **28 242 240 B**, E5 skip-heading-only **29 839 136 B**. SQLite/HNSW файлы могут менять размер после открытия/WAL checkpoint; UI показывает текущий фактический размер каталога, а не размер одного embedding payload.

Анализ section: **249 chunks <32 токенов (11.53%)**, **135 heading-only**, min=2, median=152, p90=256, max=256. Skip policy исключила ровно 135 heading-only с причинами/IDs; default include_all ничего не потерял. Разбиение/объединение исходных JSONL не выполнялось.

## Реальные RU/EN запросы и reranking

Полные компактные результаты всех **20 запросов (10 × 2 модели)**, scores, candidate counts, timings, model/index IDs и before/after: [stage2-demo-results.json](docs/stage2-demo-results.json). Веса/индексы к этому моменту сохранены, проверочный процесс запрещал любые non-loopback socket connections. Это настоящий offline-process smoke, не mocked retrieval. **Физическое отключение сети агентом: NOT TESTED**; сценарий для пользователя — Test M.

Примеры настоящей выдачи без reranking (Top-K=5, candidate pool=80, threshold=0):

| Модель | Запрос | Первый найденный документ | Cosine similarity |
| --- | --- | --- | ---: |
| E5 | How to prevent SQL injection? | SQL Injection Prevention Cheat Sheet | 0.910428 |
| E5 | Как предотвратить SQL-инъекции? | SQL Injection Prevention Cheat Sheet | 0.860006 |
| MiniLM | How to prevent SQL injection? | SQL Injection Prevention Cheat Sheet | 0.786760 |
| MiniLM | Как предотвратить SQL-инъекции? | SQL Injection Prevention Cheat Sheet | 0.784165 |

E5 вторым результатом обоих SQL-запросов вернула Query Parameterization. У MiniLM второй RU-результат — Application Logging Vocabulary; это наблюдение, а не скрытая подмена выдачи или обещание идеальной релевантности. Reranking меняет порядок на основании собственной cross-encoder модели, не на основании заранее заданных тем.

В SQL demo cross-encoder изменил позиции у **80 eligible candidates E5** и **68 MiniLM** (MiniLM: часть из 80 retrieved удалена deduplication). Before/after используют один и тот же исходный pool. Для MiniLM кандидат с original rank 14 стал rank 1 с raw logit около **7.94734**. Отрицательные logits также встречаются; никакой sigmoid/маскировки под вероятность нет. Изменение ranks **не доказывает улучшение качества**: независимая evaluation запланирована на этапе №4.

## Измеренные timings и RAM

Последний сохранённый smoke: UTC `2026-10-26T22:45:57.570846+00:00` (27 октября по Europe/Bucharest). Timings относятся к backend pipeline; сериализация HTTP и отрисовка браузера не включены. Warm averages — 10 демонстрационных queries на модель, без reranking. Это smoke на рабочем ноутбуке, не контролируемый сравнительный научный benchmark.

| Модель | Lazy load | Mean warm query | Mean query embedding | Mean Chroma retrieval | Reranking stage |
| --- | ---: | ---: | ---: | ---: | ---: |
| E5 | 6 198.79 ms | 217.27 ms | 23.93 ms | 87.60 ms | 8 542.23 ms |
| MiniLM | 1 557.27 ms | 132.98 ms | 17.63 ms | 14.25 ms | 6 721.76 ms |

Lazy load E5 включает первоначальный импорт Torch/Transformers в процессе; при переключении на MiniLM библиотеки уже загружены. Reranking stage включает lazy load отдельного reranker и inference по eligible pool. Первая UI EN-выдача после холодной загрузки сервера заняла **6 704 ms** backend total. Не интерпретировать различия как окончательный вывод о лучшей модели.

RSS после E5 load: **659 615 744 B**, после переключения на MiniLM: **824 627 200 B**. После reranking: E5 **1 214 992 384 B**, MiniLM **1 254 346 752 B**. Наибольший RSS процесса smoke при опросе каждые 50 ms: **1 254 825 984 B** (~1.17 GiB). После unload/GC осталось **556 519 424 B**: allocator/native runtime удерживают память. Это приблизительные process RSS, не сумма веса файлов и не гарантия пиков всех возможных batch sizes. Одна embedding-модель хранится в cache; reranker — отдельный optional slot. Для полного сброса — restart приложения после завершения операций.

## Автоматические и интеграционные проверки

- `pixi install --locked` — PASS.
- `pixi run test` — **121 passed, 0 failed, 0 skipped**. Все прежние **87** тестов сохранены и проходят, добавлено 34 новых cases.
- Один некритичный warning: Starlette TestClient сообщает о будущей миграции с httpx на httpx2. Он не скрывается; HTTP tests действительно выполнены.
- `pixi run lint` — PASS.
- `pixi run smoke` и `pixi run smoke --network` — PASS; реальные 55 GitHub путей подтверждены.
- `pixi run real-smoke --offline --demos` — PASS для обеих pretrained моделей, 20 RU/EN queries, нормализованных finite 384D vectors/детерминизма, Top-K 3/5/10/20, topic/threshold filtering и настоящего reranking.
- Repeated Build / Rebuild / different models / different preprocessing / skip policy — реально выполнены.
- Persistence — индексы построены в CLI, открыты в другом процессе real-smoke и повторно после restart API.
- Независимая полная проверка persisted данных — PASS: SHA256 57 raw и 8 старых JSONL, все **10 561** vectors в четырёх опубликованных коллекциях: finite/L2 norm, unique IDs, точное совпадение indexed text с char range исходного chunk, SHA256 текста и повторная фактическая токенизация полного input в пределах 512/128. Receipt: `data/diagnostics/stage2-integrity.json` (локальный, исключён из Git).

Обычный pytest не скачивает большие модели: unit mechanics проверены tiny random local BERT, integration — реальной Chroma + явно mock provider. Эти результаты отдельно от настоящего model smoke. Проверены model config/prefixes/budget, dimension/finite/normalization/determinism, missing/offline cache/switching; snapshot identity, повторный Build, crash/failed generation, resume, сохранение published pointer, stale и повреждённая collection; фильтры/дедуп, original/final rank, missing/error reranker; API JSON/422/409/404 и старые endpoints.

В ходе работы исправлены недоступная Windows pytest-current ссылка (теперь каждый run использует новый UUID-каталог в игнорируемом `.cache/tests`), конфликт имён test-модулей, диагностический счётчик reused Build и чтение временно заменяемых файлов. Неуспешные промежуточные запуски не засчитаны как PASS.

## UI и ручная приёмка

Проверка во встроенном Chromium: Dashboard, Knowledge Base, настоящий Search EN и RU, E5/MiniLM switching, Top-K=3, topic Injection (все три результата Injection), threshold=1 (честная пустая выдача), настоящий cross-encoder reranking, раскрытая before/after таблица, источники/metadata и отсутствие JS errors. Backend перезапущен с сохранёнными четырьмя индексами. Кнопки Build Index (reused) и Prepare / Verify Model реально запустили операции и завершились успешно; Inspect Index показал 135 excluded heading-only и analysis. RU с reranking вернул GraphQL/Query Parameterization/NoSQL/SQL fragments; его порядок честно отображён, без объявления улучшения качества.

Скриншоты: [RU с reranking](docs/stage2-search.jpg), [полная MiniLM RU выдача с фильтром Injection](docs/stage2-search-results.jpg). Краткие console stage logs проверены реальным CLI-запросом; подробные IDs/scores остаются в JSON-логе. Перед передачей проверочный сервер останавливается, чтобы пользователь запускал приложение самостоятельно.

Непосредственная проверка установленного Chrome/Edge, физического offline и ручного прерывания работающего Build агентом: **NOT TESTED**. Детерминированные interruption/recovery scenarios проверены integration tests, offline inference — real-model smoke с процессной сетевой блокировкой. Полная самостоятельная приёмка: [MANUAL_TESTING_STAGE2.md](MANUAL_TESTING_STAGE2.md), Tests A–R.

## Реализовано / границы

Реализованы две настоящие embedding-модели, lazy CPU inference, model download/integrity/status, analysis и policies, generation publication/recovery, настоящая Chroma, retrieval/filter/dedup/rerank/compare, Search UI, index management, локальная диагностика/console logs, API/CLI/Pixi и документация.

**NOT IMPLEMENTED (следующие этапы):** Ollama, LLM Answer Generation, AI Chat generation, Langfuse Cloud, независимая evaluation с 40 вопросами. Необязательное удаление индекса через UI также не реализовано; исходные данные и старые поколения автоматически не удаляются. Известные пределы: один inference/mutation за раз (409 при конфликте), finite candidate pool, неидеальная релевантность отдельных результатов, native allocator RSS, необходимость backup/rebuild при физических повреждениях Chroma. Полная транзакционность Chroma+catalog не обещается.

Источники решений: [Chroma Python API](https://docs.trychroma.com/reference/python), [E5 model card](https://huggingface.co/intfloat/multilingual-e5-small), [MiniLM model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2), [cross-encoder model card](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1), [задание преподавателя](https://github.com/Randwow/PAD). Принадлежность документов и CC BY-SA 4.0 атрибуция OWASP сохранены.
