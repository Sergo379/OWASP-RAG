# OWASP-RAG — Intelligent Cybersecurity Knowledge Assistant

Учебно-исследовательский проект по безопасности веб-приложений для [лабораторной работы №1 Randwow/PAD](https://github.com/Randwow/PAD#лабораторная-работа-1-разработка-собственной-rag-системы).

Реализован **этап №1: Foundation & Data Pipeline**: автоматический сбор OWASP, контроль изменений, подготовка Markdown, четыре стратегии chunking, FastAPI и локальный веб-интерфейс. Поиск и генерация ответов будут добавлены позднее. Принятие этапа пользователем — после самостоятельной ручной проверки.

```powershell
Set-Location C:\MyWorkP\OWASP-RAG\lab1
pixi install --locked
pixi run app
```

Откройте [локальный интерфейс](http://127.0.0.1:8000). В Knowledge Base нажмите «Обновить документы», затем «Запустить обработку». Первый preprocessing скачает только JSON токенизатора E5, без весов модели.

- [Подробное описание лабораторной](lab1/README.md)
- [Самостоятельное ручное тестирование](lab1/MANUAL_TESTING.md)
- [Архитектура и контракты будущих компонентов](lab1/ARCHITECTURE.md)
- [План этапов №2–4](lab1/ROADMAP.md)
- [Фактический отчёт проверок](lab1/VALIDATION.md)

Требуются Windows x64, Pixi и интернет для установки/обновления источников. Целевая машина: Windows 11, Intel Core i5-1235U, 16 GB RAM, без CUDA. Docker, Ollama и ML-фреймворки на первом этапе не требуются. Все данные остаются локально; `.pixi`, raw-корпус, tokenizer cache, processed JSONL и логи исключены из Git.

Источник: [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/), авторы — OWASP Foundation и участники проекта. Документация распространяется по [CC BY-SA 4.0](https://github.com/OWASP/CheatSheetSeries/blob/master/LICENSE.md). Преобразования: удаление front matter, HTML-комментариев вне кода, нормализация и разбиение на chunks. Производные материалы сохраняют атрибуцию и условия CC BY-SA 4.0. Проект не является официальным продуктом OWASP. Лицензия исходного кода проекта пока отдельно не назначена; лицензия данных не переносится автоматически на код.
