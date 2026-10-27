# OWASP-RAG — Intelligent Cybersecurity Knowledge Assistant

Учебно-исследовательский проект по безопасности веб-приложений для [лабораторной работы №1 Randwow/PAD](https://github.com/Randwow/PAD#лабораторная-работа-1-разработка-собственной-rag-системы).

Этапы №1–2 приняты пользователем. **Этап №3** добавляет локальную генерацию Ollama / Qwen3 1.7B и 4B, AI Chat с потоком, остановкой, историей, проверяемыми цитатами и Langfuse 5 / OpenTelemetry. Подключение Langfuse Cloud требует ключей пользователя в локальном `.env`; отсутствие Cloud не блокирует RAG. Результаты и ограничения — в отчёте этапа №3.

```powershell
Set-Location C:\MyWorkP\OWASP-RAG\lab1
pixi install --locked
pixi run app
```

Откройте [локальный интерфейс](http://127.0.0.1:8000). Knowledge Base управляет корпусом, моделями и индексами; Search выполняет retrieval; AI Chat формирует ответы по найденным фрагментам. Уже подготовленные индексы не нужно перестраивать для генерации.

- [Подробное описание лабораторной](lab1/README.md)
- [Самостоятельное ручное тестирование](lab1/MANUAL_TESTING.md)
- [Ручная приёмка этапа №2](lab1/MANUAL_TESTING_STAGE2.md)
- [Результаты реальных проверок этапа №2](lab1/STAGE2_RESULTS.md)
- [Приёмка этапа №3: сценарии A–U](lab1/MANUAL_TESTING_STAGE3.md)
- [Результаты и ограничения этапа №3](lab1/STAGE3_RESULTS.md)
- [Архитектура и контракты будущих компонентов](lab1/ARCHITECTURE.md)
- [План этапов №2–4](lab1/ROADMAP.md)
- [Фактический отчёт проверок](lab1/VALIDATION.md)

Требуются Windows x64, Pixi и интернет для установки/подготовки моделей. Для AI Chat нужен установленный [Ollama](https://ollama.com/download/windows): `ollama pull qwen3:1.7b`, `ollama pull qwen3:4b`, затем `pixi run llm-check --inference`. Целевая машина: Windows 11, Intel Core i5-1235U, 16 GB RAM, CPU. Docker не требуется. Подготовленные поиск и генерация работают локально; Cloud observability требует интернета. Данные, история чата, weights, `.env`, `.pixi`, ChromaDB и логи исключены из Git.

Источник: [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/), авторы — OWASP Foundation и участники проекта. Документация распространяется по [CC BY-SA 4.0](https://github.com/OWASP/CheatSheetSeries/blob/master/LICENSE.md). Преобразования: удаление front matter, HTML-комментариев вне кода, нормализация и разбиение на chunks. Производные материалы сохраняют атрибуцию и условия CC BY-SA 4.0. Проект не является официальным продуктом OWASP. Лицензия исходного кода проекта пока отдельно не назначена; лицензия данных не переносится автоматически на код.
