# OWASP-RAG · этап №1

Цель — создать воспроизводимый сбор и подготовку знаний о безопасности веб-приложений для последующей собственной RAG-системы. Требования: [задание преподавателя](https://github.com/Randwow/PAD). Сейчас доступны Foundation, Grabber, Preprocessing, Dashboard и Knowledge Base. Финальная система будет поддерживать вопросы на русском и английском; сейчас генерации ответов нет.

## Обоснование решений

- **OWASP**: тематически связанная техническая документация с явными рекомендациями, примерами кода и открытой лицензией. 55 выбранных материалов охватывают Authentication, Authorization, Session Management, Injection, XSS, API Security, Cryptography, Input Validation, Logging/Monitoring и Secure Configuration.
- **Python 3.11**: зрелые библиотеки HTTP/API, типизированные модели и последующая интеграция с NLP без отдельной платформы.
- **Pixi**: отдельная среда проекта, lockfile с точными версиями, единые команды PowerShell. `requirements.txt` — справочное отражение ограничений `pixi.toml`; устанавливать через pip отдельно не нужно.
- **Markdown из GitHub**: структура заголовков и code blocks, точные blob SHA и commit. Не требуется скачивать историю Git или парсить навигацию сайта.
- **FastAPI + обычные HTML/CSS/JS**: один сервер, без Electron, Node dev-server, Docker и фоновой ML-инфраструктуры.
- **Файлы + атомарные указатели**: легко показать raw, manifest и JSONL на защите; интерфейс storage можно расширить отдельным векторным хранилищем.

## Установка и запуск

Windows 11 x64, CPU, 16 GB RAM. Pixi 0.81.0 или совместимая версия. Системный Python не используется. Интернет нужен для установки пакетов, обновления OWASP и первой загрузки JSON токенизатора (~17 MB); полные веса моделей не скачиваются.

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

`src/core`, `grabber`, `preprocessing`, `storage`, `observability`, `api`, `ui` — реализованные модули. Будущие embeddings/retrieval/reranking/filters/generation/evaluation описаны контрактами в [ARCHITECTURE.md](ARCHITECTURE.md), без фиктивных функций. `data/vector_db`, `data/evaluation`, `experiments/configs/results/charts` подготовлены как каталоги.

```powershell
pixi run test
pixi run lint
pixi run smoke
pixi run smoke --network
```

Unit и integration tests используют fixtures и mocked HTTP; внешняя сеть запрещена тестовой фикстурой (локальный socketpair Windows asyncio разрешён). Покрыты первый/повторный/изменённый сбор, stable IDs, удаление, повреждение raw/manifest/JSON, 404/429/rate limit/timeout/retry, прерывание/продолжение, атомарность, очистка технических документов, токены/размеры/overlap/детерминизм, все стратегии, API, ошибки, пустой корпус, конфликты и перезапуск. [Ручные проверки](MANUAL_TESTING.md), [фактические измерения](VALIDATION.md).

JSON-логи: `lab1/logs/application.log`, ротация 2 MB × 4 файла. Поля: timestamp, level, module, operation, message, error_details. Неожиданные ошибки содержат тип и позиции вызовов без секретов, exception messages и локальных переменных. Время в файлах — UTC, UI показывает локальное время браузера. Заголовки Authorization не логируются.

Резервная копия — копия каталога `data` при остановленных операциях. Для воспроизведения другого snapshot используйте отдельный `OWASP_DATA_DIR` и сохранённый commit в corpus.yaml. Память на этапе №1 ограничена одним небольшим корпусом и одним токенизатором; embeddings/reranker/LLM не загружаются. Сохранение JSONL сейчас пакетное; для очень больших корпусов потребуется потоковая запись.

## Ограничения и дальнейшая работа

Семантический поиск, embeddings, ChromaDB, reranker, filtering, Ollama, AI Chat, Langfuse и evaluation не реализованы. UI честно показывает этапы №2–4. Нет результатов качества RAG и сравнительных выводов об оптимальной стратегии. Рабочая конфигурация — начальная, не «лучшая» по непроведённому эксперименту. Подробнее: [ROADMAP.md](ROADMAP.md).

Данные: [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/), авторы OWASP Foundation и участники. [CC BY-SA 4.0](https://github.com/OWASP/CheatSheetSeries/blob/master/LICENSE.md). При распространении raw/chunks сохраняйте атрибуцию, ссылку на источник, указание преобразований и ту же лицензию. Все изменения pipeline — подготовка/нормализация/chunking. Это самостоятельный учебный проект, не официальный продукт OWASP.
