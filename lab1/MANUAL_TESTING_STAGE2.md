# Самостоятельная приёмка этапа №2

Проверка выполняется без Codex на Windows 11, PowerShell, Chrome/Edge. Первый этап уже принят. Здесь проверяются реальные embeddings, ChromaDB, filtering, reranking и интерфейс Search. LLM ответ не генерирует. Не редактируйте Python-код и не удаляйте raw/manifest/processed.

Команды ниже запускаются из `C:\MyWorkP\OWASP-RAG\lab1`. Команды подготовки/индексирования выполняйте по одной: UI и CLI используют общий FileLock. HTTP 202 означает начало операции; успешный итог — `succeeded` в панели текущей операции. `failed`/`interrupted` не являются успехом. Примерные времена и реальные сохранённые результаты — STAGE2_RESULTS.md; scores не обязаны совпадать до последней цифры на другом CPU.

Перед проверкой сохраните `git rev-parse HEAD`, source commit из Dashboard и screenshots/JSON результатов в своей папке для защиты. Идентификаторы индексов можно скопировать из Inspect Index или `pixi run indexes`.

## Test A — Installation

1. **Открыть:** VS Code → Open Folder → `C:\MyWorkP\OWASP-RAG`; Terminal → PowerShell.
2. **Команды:**
   ```powershell
   Set-Location C:\MyWorkP\OWASP-RAG\lab1
   pixi install --locked
   pixi run smoke
   pixi run app
   ```
3. **Выбрать/нажать:** в Chrome/Edge открыть [http://127.0.0.1:8000](http://127.0.0.1:8000), Dashboard → Knowledge Base. Текст вводить не требуется.
4. **Ожидается/успех:** версия 0.2.0, старые счётчики/документы доступны; есть CPU Models и Vector Index Management. Сервер слушает только loopback, local smoke passed.
5. **При ошибке:** проверьте папку и интернет для install. Занятый 8000 — остановите свой старый сервер Ctrl+C. Не завершайте неизвестные процессы. **Для защиты:** версия Pixi, вывод установки/smoke, Dashboard.

## Test B — Model Download

1. **Открыть:** Knowledge Base → CPU Models. Нажмите Prepare Model у E5, дождитесь succeeded; затем MiniLM, затем Reranker. Если модели уже готовы, Prepare / Verify Model проверит файлы и inference повторно.
2. **Выбрать/ввод:** модели E5, MiniLM и cross-encoder; текст запроса пока не вводится.
3. **CLI-эквивалент** (по одному, не во время UI-операции):
   ```powershell
   pixi run prepare-model --model e5
   pixi run prepare-model --model minilm
   pixi run prepare-model --model reranker
   pixi run models
   ```
4. **Ожидается/успех:** фактический статус загрузки и число bytes/файлов, затем ready. Около 488/480/488 MB decimal соответственно; SHA256 закреплён. Итог `inference_verified: true`. E5/MiniLM dimension 384; MiniLM input budget 128. Ready на карточке означает готовые локальные файлы; «в памяти» показывается отдельно.
5. **При ошибке:** восстановите интернет и повторите Prepare; уже подтверждённые файлы используются повторно. Не путайте старый E5 tokenizer этапа №1 с полными weights. **Для защиты:** revisions, итог JSON, download/load durations и RSS; токены не публикуйте.

## Test C — ChromaDB

1. **Открыть:** Knowledge Base → Vector Index Management.
2. **Выбрать:** Embedding Model = E5, Preprocessing = section / 256 / 0%, Heading policy = include_all, Token overflow = split. **Нажать:** Build Index.
3. **Команда-эквивалент:**
   ```powershell
   pixi run build-index --model e5 --configuration-id 95c6ca90edb66fce3ee9
   pixi run indexes
   ```
4. **Ожидается/успех:** Building → Ready, реальный прогресс batches/vectors/source chunks, затем 384D collection. Inspect Index показывает identity, collection_name, snapshot, число векторов/документов, elapsed и disk size. Повторный Build существующего готового identity может сразу вернуть `reused:true`.
5. **При ошибке:** missing weights → Test B; несовпадение preprocessing с корпусом → повторите эту конфигурацию preprocessing и Build. Исходные chunks автоматически не изменяются. **Для защиты:** index ID, commit/fingerprint, конфигурация и итоговый JSON. Поиск идёт в настоящую ChromaDB, не JSON-список векторов.

## Test D — Search EN

1. **Открыть:** Search. **Выбрать:** E5, готовый section-индекс, Top-K=5, Threshold=0, все темы/документы, reranking выключен.
2. **Ввести:** `How to prevent SQL injection?` **Нажать:** Search или Enter.
3. **CLI:**
   ```powershell
   $idx = 'СКОПИРУЙТЕ_ПОЛНЫЙ_INDEX_ID'
   pixi run search --model e5 --index-id $idx --query 'How to prevent SQL injection?' --top-k 5
   ```
4. **Ожидается/успех:** реальные фрагменты, связанные с SQL injection/parameterized queries; title, источник, chunk ID, heading_path, cosine similarity/distance. Полный chunk и metadata раскрываются. Точное ранжирование — не фиксированный эталон.
5. **При ошибке:** нет индекса → Test C; missing model → Test B; Busy → дождитесь операции. **Для защиты:** 5 результатов, settings и Search Execution Details. Не ожидайте сформулированного ответа LLM.

## Test E — Search RU

1. **Открыть:** ту же Search page с теми же параметрами.
2. **Ввести:** `Как предотвратить SQL-инъекции?` **Нажать:** Search.
3. **Команда:** `pixi run search --model e5 --index-id $idx --query 'Как предотвратить SQL-инъекции?'`.
4. **Ожидается/успех:** найден англоязычный OWASP-контекст по смыслу русского вопроса, есть технические рекомендации/источники. Сравните с EN, не требуя одинаковых scores.
5. **При ошибке:** проверьте модель/индекс и фильтры. **Для защиты:** обе выдачи RU/EN, query embedding timing и source snapshot.

## Test F — Top-K

1. **Открыть:** Search; E5, без фильтров, Threshold=0, reranking off. **Ввести:** `How should passwords be stored securely?`.
2. **Выбрать/нажать:** поочерёдно Top-K 3, 5, 10, 20 → Search. Candidate Pool Size оставьте 80.
3. **Команда:** `pixi run search --model e5 --index-id $idx --query 'How should passwords be stored securely?' --top-k 20 --candidate-pool 80`.
4. **Ожидается/успех:** при достаточном числе кандидатов возвращается соответствующее K; при фильтрах/дедупликации допустимо меньше, без искусственного заполнения. Диагностика различает candidate pool и final count. Для K=20 пул должен быть ≥40.
5. **При ошибке:** слишком маленький pool выдаёт validation error, увеличьте его. **Для защиты:** final counts и применённые параметры четырёх запусков.

## Test G — Filtering

1. **Открыть:** Search, вопрос `How to prevent SQL injection?`, Top-K=5, pool=80.
2. **Выбрать/нажать:** сначала Topic=Injection, Threshold=0 → Search; затем другой Topic; затем Threshold=1 → Search. Отдельно выберите Source / Document = SQL Injection Prevention и повторите поиск. Верните фильтры после проверки.
3. **Команда:** `pixi run search --model e5 --index-id $idx --query 'How to prevent SQL injection?' --topic Injection --threshold 0`.
4. **Ожидается/успех:** metadata выдачи соответствует выбранной теме/документу. Threshold фильтрует cosine similarity (1−distance), при 1 обычно результатов нет. Диагностика сообщает removed/reasons, actual count. Metadata также применяется внутри Chroma, чтобы пул был выбран среди подходящих документов.
5. **При ошибке:** строгие фильтры могут законно дать пустую выдачу; снимите их. **Для защиты:** сравнение настроек/выдач и removed candidates. Scores reranker не используются как similarity threshold.

## Test H — Reranking

1. **Открыть:** Search; E5 section, тот же SQL-вопрос, Top-K=5, pool=80, без metadata-фильтров.
2. **Выбрать/нажать:** включите Enable Reranking → Search. Reranker должен быть ready из Test B.
3. **Команда:** `pixi run search --model e5 --index-id $idx --query 'How to prevent SQL injection?' --reranking`.
4. **Ожидается/успех:** реальные raw logits, original rank и final rank. Раскройте Without Reranking / With Reranking: обе выдачи построены из одного набора кандидатов в одном запросе. Порядок может измениться; это ещё не доказательство улучшения качества. Logit может быть отрицательным и не является вероятностью. Длинные тексты явно разбиваются на окна reranker, aggregation=max_raw_logit.
5. **При ошибке:** prepare reranker, сократите query до ≤128 его токенов. При выключенном reranker поиск должен работать независимо от него. **Для защиты:** before/after, comparison table, rerank timing и transform metadata.

## Test I — Index Persistence

1. **Открыть:** Inspect Index, запишите index ID и vectors_count. Завершите все операции.
2. **Команды:** остановите сервер Ctrl+C, затем `pixi run app`; обновите страницу.
3. **Выбрать/ввести/нажать:** прежний индекс, тот же EN/RU-вопрос → Search.
4. **Ожидается/успех:** индекс Ready, количество векторов то же, поиск работает без повторного Build. Первый запрос после restart загружает модель, следующие используют память процесса.
5. **При ошибке:** используйте тот же OWASP_DATA_DIR; проверьте наличие `data/vector_db`. **Для защиты:** counts/ID до и после и timings cold/warm. При смене snapshot статус может стать Stale — это отдельное предупреждение.

## Test J — Duplicate Prevention / Rebuild

1. **Открыть:** Knowledge Base → тот же model/config/policy; нажмите Build Index повторно.
2. **Ожидается:** тот же index_id, `reused:true`, `computed_vectors:0`, число векторов не выросло. **Команда:** повторите команду Test C.
3. **Нажать:** Rebuild Index в строке индекса, дождитесь Ready. Ввод текста не требуется.
4. **Успех:** новое поколение collection, прежний logical index ID и тот же vectors_count; cache_hits позволяет не пересчитывать неизменившиеся embeddings. Inspect показывает новую duration. При ошибке нового поколения старое опубликованное поколение остаётся searchable.
5. **При ошибке/прерывании:** Build с тем же выбором продолжит незавершённую сборку. Не очищайте Chroma каталог вручную при работающем сервере. **Для защиты:** JSON repeat/rebuild, generation, counts, computed/cache hits.

## Test K — Multiple Models

1. **Открыть:** Knowledge Base → Build Model = MiniLM, прежняя section-конфигурация, include_all, split → Build Index.
2. **Команда:** `pixi run build-index --model minilm --configuration-id 95c6ca90edb66fce3ee9`.
3. **Выбрать/ввести/нажать:** Search → MiniLM → его индекс → RU и EN SQL-вопросы → Search. Затем вернитесь на E5 и повторите.
4. **Ожидается/успех:** отдельные IDs/collections, правильный model_identity. MiniLM использует свой tokenizer и лимит 128; поэтому векторов может быть больше исходных chunks. Metadata содержит origin chunk_id, char_start/end, transformation. E5-prefix к MiniLM не добавляется. В памяти остаётся одна embedding-модель; отдельный reranker возможен при включении.
5. **При ошибке:** не выбирайте E5-индекс с MiniLM; API отклонит несовпадение. **Для защиты:** два index identities, исходные/индексированные counts, реальные queries/timings, RSS из диагностики.

## Test L — Multiple Chunking Configurations / Short Chunks

1. **Открыть:** Knowledge Base. Выберите paragraph / 256 / 0% в preprocessing и запустите, если этой конфигурации нет. Старый section JSONL сохраняется.
2. **Выбрать/нажать:** Vector Index Management → E5 → paragraph → include_all → Build. Затем выберите section и policy=skip_heading_only → Build.
3. **Команды:**
   ```powershell
   pixi run build-index --model e5 --configuration-id 9488c1f4e057ae271e0b
   pixi run build-index --model e5 --configuration-id 95c6ca90edb66fce3ee9 --heading-policy skip_heading_only
   ```
4. **Ввод/успех:** выполните одинаковый SQL-вопрос в новых индексах. Индексы отличаются по identity. Анализ показывает min/median/p90/max, число/процент коротких chunks и heading_only. Skip исключает только целиком заголовочные fragments; Inspect показывает IDs и причины. Исходные JSONL и chunks не изменяются, автоматического объединения нет.
5. **При ошибке:** обновите нужный preprocessing при изменении корпуса. **Для защиты:** analysis, excluded_count/reasons, два chunking IDs, сравнение выдач без выводов о «лучшей» стратегии.

## Test M — Offline Search

1. **Подготовка:** модели ready, индексы Ready; завершите операции. Отключите интернет обычным способом. Перезапустите сервер, чтобы проверить холодную offline загрузку.
2. **Открыть/выбрать:** локальный Search, оба embedding-индекса по очереди. **Ввести:** RU и EN вопросы; **нажать:** Search, затем включить уже скачанный reranker и повторить.
3. **Дополнительная команда:** `pixi run real-smoke --offline` блокирует внешние socket connections только внутри проверочного процесса; это не переключение системных сетевых настроек.
4. **Ожидается/успех:** настоящие результаты из Chroma, без внешних API/скачивания. Вопросы остаются локально. Ссылки на сайт OWASP offline могут не открываться — это не ошибка поиска.
5. **При ошибке:** верните интернет и подготовьте недостающие weights, затем повторите offline. **Для защиты:** снимок успешной выдачи после restart offline, вывод smoke; отдельно укажите, было физическое отключение сети или только процессная блокировка.

## Test N — Model Download Error

1. **Открыть:** отдельный пустой каталог данных без удаления основного. Остановите сервер, выполните:
   ```powershell
   $env:OWASP_DATA_DIR = 'C:\MyWorkP\OWASP-RAG\lab1\data\stage2-offline-check'
   pixi run app
   ```
2. **Выбрать/нажать:** при отключённом интернете Knowledge Base → CPU Models → Prepare Model E5. Текст вводить не требуется.
3. **Ожидается/успех:** bounded retries, затем failed/error с предложением проверить интернет; нет ложного Ready. Основные данные/индексы не затронуты.
4. **При ошибке восстановления:** включите интернет; повторная Prepare должна работать. После теста Ctrl+C, `Remove-Item Env:OWASP_DATA_DIR`, затем `pixi run app` для основного корпуса. Во втором терминале переменную устанавливайте отдельно.
5. **Для защиты:** ошибка download, статус missing/error и успешное восстановление. Не удаляйте основную модель ради этой проверки.

## Test O — Empty/Invalid Query

1. **Открыть:** Search, готовый индекс. **Ввести/нажать:** пустую строку, затем пробелы → Search. Попробуйте Top-K=20 и pool=20; Threshold вне −1..1.
2. **Команда:** `pixi run search --model e5 --index-id $idx --query '   '`.
3. **Ожидается:** HTML validation или понятная API/CLI ошибка. Никаких фиктивных результатов, код CLI 1. API возвращает 422 для некорректных схем. Слишком длинный запрос для tokenizer отклоняется без усечения.
4. **Успех:** после исправления параметров поиск снова работает без restart. Существующий индекс не меняется.
5. **При ошибке:** посмотрите пояснение поля, верните pool=80 и threshold=0. **Для защиты:** validation message и следующий успешный запрос.

## Test P — Logs

1. **Открыть:** PowerShell, где работает `pixi run app`. На Search выполните один запрос с reranking и один без него.
2. **Ожидается:** QUERY_RECEIVED → QUERY_VALIDATED → MODEL_READY → QUERY_EMBEDDED → VECTOR_RETRIEVAL_STARTED/COMPLETED → FILTERING_COMPLETED → DEDUPLICATION_COMPLETED → RERANKING_STARTED/COMPLETED либо SKIPPED → RESULTS_READY; один request_id и реальные timings/counts/IDs/scores.
3. **Команда во втором PowerShell:**
   ```powershell
   Get-Content .\logs\application.log -Tail 20
   ```
4. **Успех:** те же события присутствуют в JSON-логах. Полный query по умолчанию не записывается в постоянный лог (`log_queries:false`); diagnostics response содержит query только для пользователя, хранится в памяти до 10 минут/20 запросов. Токенов/Authorization в логах нет.
5. **При ошибке:** проверьте retrieval.yaml/logging.yaml и restart после изменения конфигурации. **Для защиты:** последовательность событий с одним request_id и без секретов.

## Test Q — Regression этапа №1

1. **Открыть/нажать:** Dashboard и Knowledge Base; документы/источники/raw/chunks должны быть доступны. Повторите Grabber, затем выбранный preprocessing. Используйте старую MANUAL_TESTING.md для полного сценария первого этапа.
2. **Команды:** `pixi run grab`, `pixi run preprocess`, `pixi run stats`, `pixi run smoke` (по одной, не во время Search/Build).
3. **Ввод/выбор:** прежние Markdown и JSONL, без ручной правки кода. 55 активных документов, исторические 2 out_of_scope остаются.
4. **Ожидается/успех:** дубликатов нет. Если upstream изменился, числа могут измениться законно. Старый индекс станет Stale; поиск удалит неактуальные источники из выдачи, новый Build создаст другое identity для актуального snapshot. Старый опубликованный индекс не удаляется.
5. **При ошибке:** сначала устраните сеть/config/preprocessing mismatch. **Для защиты:** source commits до/после, counters, stale warning и успешный новый индекс.

## Test R — Automated / Real-model Smoke

1. **Открыть:** PowerShell в lab1. Дождитесь конца операций.
2. **Команды:**
   ```powershell
   pixi run test
   pixi run lint
   pixi run smoke
   pixi run real-smoke --offline --demos
   ```
3. **Ожидается:** unit/integration passed, lint clean, local smoke passed. Real smoke использует **реальные готовые weights и Chroma**, проверяет обе модели, RU/EN, Top-K, filtering и reranking. Его результаты сохраняются в `data/diagnostics/stage2-real-smoke.json`; он не запускается внутри обычного pytest.
4. **Успех:** код каждой команды 0 (`$LASTEXITCODE` сразу после неё), нет skipped/failed под видом passed. Демонстрационные вопросы — не независимая научная evaluation. Реальная модель и mock-тесты разделены.
5. **При ошибке:** выполните Test B/C для обеих моделей. Не ослабляйте проверки; сохраняется отдельный failed-report. **Для защиты:** версии, итоговые строки тестов, JSON smoke с timings, index IDs, реальные найденные документы и before/after reranking.

## Recovery и ограничения

Не выполняйте ручные файловые операции в Chroma при запущенном сервере/CLI. При сбое обычного Build повторите Build с теми же настройками — staging generation и вычислительный кеш позволяют продолжить. При ошибке Rebuild прежний published generation остаётся доступным. `Inspect Index` проверяет фактический count.

Если ChromaDB не открывается из-за повреждения: остановите все процессы приложения, **сначала скопируйте весь `data/vector_db` в резервную папку**. При необходимости сохраните проблемный `data/vector_db/<index_id>/chroma` переименованием в `chroma.backup-<дата>` внутри того же каталога, затем используйте Rebuild (не обычный повторный Build) для этого индекса. Сохранённый каталог не удаляйте. Исходные `raw`, `processed`, `manifests` не трогайте. Chroma не обещает транзакцию между collection и каталогом публикации; наш слой публикует только проверенное поколение, а восстановление физических повреждений требует резервной копии/rebuild. Embedding cache — ускорение, не замена Chroma; его повреждение устраняется после резервного копирования отдельным сохранением `embedding_cache.sqlite` и новым Build/Rebuild.

Удаление индекса через UI не реализовано: оно необязательно в ТЗ, автоматически данные не удаляются. Поколения сохраняются и занимают дополнительное место. Удаление Python-ссылок при переключении модели не гарантирует немедленного возврата всей RSS ОС: PyTorch/Chroma могут удерживать память. Безопасный способ полностью очистить процесс — дождаться операций, Ctrl+C и `pixi run app`; дисковые модели/индексы сохранятся.

Финальная приёмка этапа №2 выполняется пользователем после этих проверок. Ollama, LLM Answer Generation, AI Chat и Langfuse относятся к этапу №3; оценка качества на независимом наборе вопросов — к этапу №4.
