# OWASP-RAG — Intelligent Cybersecurity Knowledge Assistant

Учебно-исследовательский проект по безопасности веб-приложений для [лабораторной работы №1 Randwow/PAD](https://github.com/Randwow/PAD#лабораторная-работа-1-разработка-собственной-rag-системы).

Первый этап принят пользователем. Реализован **этап №2: локальный семантический поиск**: реальные CPU embeddings E5/MiniLM, persistent ChromaDB, фильтры, cross-encoder reranking и Search UI. Технические проверки выполнены; самостоятельная приёмка этапа №2 — по инструкции. Генерация ответов LLM и Langfuse относятся к этапу №3.

```powershell
Set-Location C:\MyWorkP\OWASP-RAG\lab1
pixi install --locked
pixi run app
```

Откройте [локальный интерфейс](http://127.0.0.1:8000). Knowledge Base управляет корпусом, моделями и индексами. Для поиска: Prepare Model → Build Index → Search. Подробная самостоятельная проверка — в инструкции этапа №2.

- [Подробное описание лабораторной](lab1/README.md)
- [Самостоятельное ручное тестирование](lab1/MANUAL_TESTING.md)
- [Ручная приёмка этапа №2](lab1/MANUAL_TESTING_STAGE2.md)
- [Результаты реальных проверок этапа №2](lab1/STAGE2_RESULTS.md)
- [Архитектура и контракты будущих компонентов](lab1/ARCHITECTURE.md)
- [План этапов №2–4](lab1/ROADMAP.md)
- [Фактический отчёт проверок](lab1/VALIDATION.md)

Требуются Windows x64, Pixi и интернет для установки/подготовки моделей. Целевая машина: Windows 11, Intel Core i5-1235U, 16 GB RAM, без CUDA. Docker и Ollama не требуются. Готовый поиск работает offline. Данные, weights, `.pixi`, ChromaDB и логи исключены из Git.

Источник: [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/), авторы — OWASP Foundation и участники проекта. Документация распространяется по [CC BY-SA 4.0](https://github.com/OWASP/CheatSheetSeries/blob/master/LICENSE.md). Преобразования: удаление front matter, HTML-комментариев вне кода, нормализация и разбиение на chunks. Производные материалы сохраняют атрибуцию и условия CC BY-SA 4.0. Проект не является официальным продуктом OWASP. Лицензия исходного кода проекта пока отдельно не назначена; лицензия данных не переносится автоматически на код.
