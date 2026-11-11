# Stage 3 Quality Fix — фактические результаты

Проверка 3 ноября 2026 года, Windows, Python 3.11/Pixi, CPU i5-1235U, 16 GB RAM. Исходный commit `90524f1bb39ae8c17b75d53b5a69518e814cc45b`. Это исправление Stage 3 на фиксированных публичных вопросах, не Stage 4 и не независимый benchmark. Статус `completed`/существующие citations не используются как оценка смысловой правильности.

## 1. Подтверждённые проблемы

В исходном `construct_prompt` краткий контракт 2–4 пункта применялся ко всем режимам. Strict и concise пользовались общим выходным лимитом 512. Проверка citations в основном проверяла IDs; Secure-cookie ошибка проходила с формально существующим источником. UI показывал процитированные документы без отдельного обзора всех кандидатов/переданного контекста.

В реальном baseline E5/1.7B на вопрос Secure ответ содержал «`Secure` шифрует содержимое cookie» и получил `completed`. Полный ответ, параметры, пул, контекст, usage и timing сохранены в [before JSON](docs/stage3-quality-before.json). Это подтверждённая ошибка, а не предположение по prompt.

## 2. Диагностика retrieval

Вопрос: «Какие меры рекомендует OWASP для защиты конфиденциальных данных при передаче через интернет?» Baseline: pool 80, Top K 5, threshold 0, без topic/document filters, section/include_all индексы. E5 и MiniLM проверены с reranking OFF и ON.

TLS Cheat Sheet был в dense pool **обеих** моделей: раздел Test the Server Configuration на rank 30 E5 и rank 56 MiniLM. Он не попал в Top 5; специализированные разделы protocols/ciphers не присутствовали в этом dense pool. Baseline E5 Top 5 включал Transaction Authorization, Authorization, CSP, Session Management; MiniLM — Session Management, Web Service, GraphQL и MFA. Optional reranking вывел Web Service Transport Confidentiality и Session TLS, но не восстановил отсутствующие protocol/cipher разделы. Поэтому «документ не процитирован» нельзя заменять утверждением «документа не было в pool».

Не выбраны переиндексация, смена E5/MiniLM, принудительный reranker или просто увеличение pool. Чистый BM25/RRF тоже оказался недостаточен: начальный объединённый Top 10 широкого вопроса не содержал TLS. В итоговую явную стратегию Chat добавлены bilingual terms, affinity по названию документа, покрытие запрошенных тем и приоритет точного раздела именованного механизма. Baseline Search, pinned model identities, коллекции и исходные cosine scores сохранены.

## 3. Реализация и причины выбора

`hybrid_context` объединяет semantic pool и BM25 существующих published chunks, RRF k=60, ограничивает итоговый pool прежним размером и выбирает контекст. Это эвристика для Chat, видимая в UI/CLI/API; baseline можно выбрать явно. Старый API без нового поля сохраняет baseline. Threshold/active/raw-SHA/topic/document/model/index ограничения действуют; rereanking остаётся отдельной опцией. Cross-encoder при включении оценивает объединённый pool один раз.

Отдельно видны semantic candidates, fused candidates, removed reasons, selected context, excluded по budget и cited sources. Лексический snapshot читается из текущего опубликованного Chroma generation и sidecar; новых embeddings, persistent индексов и загрузок weights нет. Cache держит один index/generation.

## 4. Генерация и лимиты

| Режим | Context window | Output cap | Source cap |
|---|---:|---:|---:|
| concise_grounded | 4096 | 512 | 2400 |
| strict_grounded | 4096 | 896 | 2400 |
| technical_grounded | 6144 | 1536 | 3600 |

Все значения — Qwen tokens. CPU 4 threads, temperature .1, keep_alive 0, reserve 192, история 400/2 пары; Thinking 4B сохраняет прежний override 2048/.6. Strict адаптирует глубину, technical требует конкретных поддержанных параметров и действий, concise сохраняет краткость. Явная просьба «кратко» выбирает краткий профиль. Тест проверяет фактический Ollama HTTP payload 6144/1536, а не только Pydantic/YAML.

Проверяется **весь** prompt с инструкциями, историей и output reserve. Chunks целые, исключения имеют причины. Markdown HTTP destinations убираются только в тексте для prompt с сохранением labels; transformation явная, оригинал доступен. Это уменьшает воспроизведение/выдумывание моделью URL и не меняет корпус.

При конкретной ошибке validation разрешено одно исправление той же локальной моделью по тому же контексту; оно опционально и видно в trace. Нет второго judge на каждом запросе. Повтор ограничен оставшимся timeout/prompt budget, не выполняется при adequacy gate, sentinel или незавершённом ответе. Условия отказа не ослабляются ради зелёного статуса. UI сбрасывает отклонённый provisional draft. Неудача валидации отделена сообщением от недостатка документов.

## 5. Grounding и границы проверки

Проверяются ID текущего request, непустой source text, неизвестные citations, язык, citations factual blocks, URL, exact quotations, известное смешение Secure с шифрованием значения cookie, технические literals в цитируемых chunks. Evidence показывает буквальные char spans/chunk IDs и lexical overlap для ручного сопоставления. Source URLs берутся из metadata, не из генерации.

Это **не NLI и не доказанная faithfulness**. Неверный причинный вывод с корректными словами/ID может пройти; перевод, роль механизма, неоднозначность и неполнота требуют чтения. Regex Secure покрывает известную семью ошибок, не все перефразирования. Строгий literal/quote check может отказать и на допустимой переформулировке. Не добавлены тяжёлый обязательный judge, платный API или шаблонные ответы на демонстрационные вопросы.

## 6. Неудачные итерации, сохранённые без ретуши

| Прогон | Secure | TLS technical | Что выяснено |
|---|---|---|---|
| [Attempt 1](docs/stage3-quality-attempt1.json) | refused | failed / length 1536 | Заголовок Cookie Theft давал лишний контекст; повторяющийся формат раздувал ответ, модель выдумывала URL |
| [Attempt 2](docs/stage3-quality-attempt2.json) | refused | refused | Контекст TLS стал релевантным; остались отсутствие citations и неверная роль Secure |
| [Attempt 3](docs/stage3-quality-attempt3.json) | failed / repair length 512 | refused после repair | Одно исправление не гарантирует успех; нужны явный RU citation-контракт и точные control sections |

В Attempt 1 TLS generation ~216.6 s / E2E ~223.3 s; в Attempt 2 TLS ~117.3 s / E2E ~123.7 s; в Attempt 3 TLS две попытки ~145.8+100.6 s / E2E ~258.8 s. Они относятся к разным промежуточным prompt, не являются корректным performance A/B. Отказ безопаснее принятия ошибочного черновика, но не является содержательным успешным ответом.

## 7. Итоговые реальные прогоны

Последняя серия генерации перед уточнением выбора документов сохранила 5 из 20 запросов. Secure/E5 корректно объяснил ограничение передачи cookie через HTTPS за 130,8 с. Технический TLS/E5 достиг таймаута за 360,8 с. Два широких ответа завершились за 103,1 с и 114,1 с, но включали соседние темы и неточные формулировки. TLS/MiniLM завершился за 205,6 с, правильно указал версии, Secure, доверенный CA и HSTS, однако не раскрыл проверку конфигурации и добавил слабосвязанный совет про API-ключи. Полная приёмка качества генерации по этой серии не пройдена. [Записи запросов](docs/stage3-quality-last-generation.json).

Последующий retrieval-прогон сохранил по 80 dense-кандидатов для обоих embeddings. Фокус по совпадению заголовка привёл все пять выбранных фрагментов широкого вопроса к специализированному TLS Cheat Sheet. Без reranking в пятёрку вошли настройка сервера, вводная, кеширование, протоколы и HSTS. С reranking протоколы и HSTS уступили другим TLS-разделам. Настройка `chat_document_focus` отключает сужение выбора контекста, а причины отбора доступны в diagnostics. Изменение отбора проверено отдельно от генерации. [Четыре retrieval-записи](docs/stage3-quality-retrieval.json).

Автоматические проверки после этого изменения: lint, 228 тестов. Предшествующий прогон прошёл Stage 1 smoke и Stage 2 real smoke с локальными моделями и опубликованными коллекциями. Результаты дальнейших сравнений модели и независимой выборки ведутся в Stage 4.

Публичные JSON хранят длинные фрагменты по SHA-256. Поля `*_text_sha256` разрешаются в `source_texts` того же файла.

## 8. Регрессии, offline и observability

Прежние 171 тест сохранены; новые проверяют режимы/HTTP payload, полный prompt budget, literals/quotes/source binding, Secure positive/negative cases, BM25, сохранение baseline IDs/scores/catalog/count, filters, optional reranker и ограниченное условное исправление. Mock tests проверяют поведение кода; реальные pinned weights/Ollama/Chroma проверяются отдельными smoke.

Langfuse SDK 5.0.4 + OTel сохранены. Новые retrieval/repair шаги — дочерние spans того же request trace. При metadata-only evidence quotes и rejected drafts не экспортируются. Cloud delivery без пользовательских ключей остаётся непроверенной, локальный SDK не выдаётся за Cloud. Offline smoke блокирует внешний socket в Python, Ollama loopback разрешён; физическое отключение сети отдельно не заявляется.

## 9. Производительность и сохранение данных

CPU inference последовательный. В реальных JSON есть E2E, TTFT, prompt/eval counts/durations, effective budgets, peak Python/Ollama/combined RSS и minimum available RAM. Summed RSS может считать shared pages дважды и не равен общему потреблению системы. Cold embedding load значительно дороже повторного query; CPU-сравнение latency зависит от состояния кеша и фоновой нагрузки.

Ни build/rebuild, ни изменение weights для этого исправления не выполнялись. Контроль published indexes: E5 section include_all 2159/g2, paragraph 2323/g1, skip_heading 2024/g2; MiniLM section 4055/g1; всего 10561. Проверка фактического сохранения и smoke приведена ниже.

## 10. Ручная приёмка и оставшиеся ограничения

[Пошаговая инструкция](MANUAL_TESTING_STAGE3_QUALITY_FIX.md) содержит команды, точные настройки/вопросы, критерии ошибки, diagnostics, сохранение логов и offline. Пользователю нужно проверить содержательную правильность, полноту сертификатов/проверок TLS, отдельные причинные выводы и параметры; просмотреть selected и cited отдельно; проверить Stop, историю и альтернативную 4B Instruct на своей нагрузке.

Крупный русский инженерный вопрос остаётся трудным для 1.7B. Увеличение token cap не обеспечивает качество; repair повышает задержку и может отказать. Hybrid metadata affinity может выбрать соседнюю тему или недостаточно полное покрытие, особенно при неоднозначном широком вопросе. Полного semantic judge и независимой оценки всего корпуса здесь нет. Thinking 4B не рекомендован для обычного CPU Chat из-за известных задержек/length, и его weights/template не подменены.
