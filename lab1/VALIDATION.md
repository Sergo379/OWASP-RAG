# История проверок OWASP-RAG

**Актуальное состояние:** этап №1 принят пользователем (подтверждено в ТЗ этапа №2), включая Chrome/Edge, restart и реальное отключение интернета. Новые результаты этапа №2 находятся в [STAGE2_RESULTS.md](STAGE2_RESULTS.md), ручная приёмка — [MANUAL_TESTING_STAGE2.md](MANUAL_TESTING_STAGE2.md). Ниже сохранён исторический отчёт разработки этапа №1 без подмены его измерений новыми.

## Фактическая проверка этапа №1 (исторический отчёт)

Дата: 26 октября 2026. Windows x64, Pixi 0.81.0, отдельная среда Python 3.11 из `pixi.lock`. Это техническая проверка подготовки данных; evaluation качества RAG не проводилась. Итоговое принятие пользователем ещё не выполнено.

## Источник и данные

- Repository: `OWASP/CheatSheetSeries`.
- Source commit: `a5fbb7f886c02e3418e17f5525874a868cca49e4`.
- Итоговая конфигурация: 55 активных тематических документов, все пути существуют.
- Первоначально скачано 55 документов. После чтения заголовков два DEPRECATED-материала заменены: Access Control → Authorization Patterns; TLS Cipher String → Post Quantum Cryptography.
- При обновлении после замены: new=2, unchanged=53, errors=[]. Старые копии не удалялись; manifest хранит всего 57 документов, из них 2 out_of_scope, 55 active.
- Повторный запуск итоговой выборки: new=0, changed=0, unchanged=55, deleted=0, errors=[]. Дубликаты не появились.
- Токенизатор: `intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3`, JSON SHA256 `0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39`. Веса ML-модели не загружались.

| Стратегия | Размер | Overlap | Документов | Chunks |
| --- | ---: | ---: | ---: | ---: |
| fixed | 256 | 0% | 55 | 1 313 |
| overlap | 256 | 20% | 55 | 1 614 |
| paragraph | 256 | 0% | 55 | 2 323 |
| section (основная) | 256 | 0% | 55 | 2 159 |

Все четыре запуска завершились succeeded. Независимая проверка всех 7 409 chunks подтвердила совпадение token_count с повторной токенизацией текста, token_count в пределах 1..chunk_size, правильный SHA256 текста и уникальные chunk IDs. Проверена SHA256-целостность 57 сохранённых raw. Это не сравнительный эксперимент по качеству retrieval.

Основной configuration_id: `95c6ca90edb66fce3ee9`. Повторный preprocessing через UI сформировал тот же JSONL/hash. Разные стратегии находятся в разных JSONL; указатель latest снова section.

## Окружение и автоматические проверки

- `pixi install --locked` — выполнено успешно.
- `pixi run lint` — выполнено успешно.
- `pixi run smoke` — локальная проверка выполнена успешно.
- `pixi run smoke --network` — local=passed, network=passed, проверены все 55 путей.
- `pixi run app` — FastAPI действительно запущен на `127.0.0.1:8000`; страницы и endpoints отвечают.
- `pixi run test` — **87 passed, 0 failed, 0 errors**, 2.92 s в финальном запуске. Unit и integration выполнены, пропущенных тестов нет.

В ходе разработки исправлены ограничения временной папки sandbox (тесты выполнены с разрешённым доступом к штатному Windows temp), несовместимость первоначального запрета всех socket-соединений с Windows asyncio socketpair и sharing violation при атомарной замене operation.json во время чтения. Ранние неуспешные запуски не считались успешными. После исправлений suite повторно выполнен.

## Проверка интерфейса

Проверен встроенный Chromium-браузер: Dashboard, Knowledge Base, фильтр по SQL Injection, открытие реального Markdown/chunks с heading_path и token_count. Кнопки Grabber и Preprocessing запустили настоящие операции; видны running и отключённые кнопки, затем succeeded. Корпус и результат сохранились после перезапуска backend. Проверены узкий вид и desktop breakpoint 1440 px. [Скриншот Dashboard](docs/dashboard.jpg).

Доступ к отдельному окну установленного Chrome завершился тайм-аутом ожидания разрешения UI-инструмента. Проверка именно Chrome/Edge остаётся пользователю по MANUAL_TESTING.md. Настоящее физическое отключение интернета не выполнялось; ошибки сети и восстановление проверены mocked integration/unit tests. Сетевая успешная smoke-проверка была реальной.

## Воспроизведение

Все команды выполняются из `lab1`. `pixi run test`, `pixi run lint`, `pixi run smoke --network`, `pixi run grab`, `pixi run preprocess`, `pixi run stats`. Чтобы повторить именно этот snapshot, сохраните копию corpus.yaml, замените его `ref` на source commit выше, используйте отдельный `OWASP_DATA_DIR`, затем перезапустите сервер/CLI. Нельзя сравнивать счётчики для разных source snapshots как один и тот же эксперимент.

Никаких embeddings, ChromaDB, reranker, LLM, Langfuse traces или итоговых evaluation-метрик в этом этапе нет. План — ROADMAP.md. Полная самостоятельная приёмка — MANUAL_TESTING.md.
