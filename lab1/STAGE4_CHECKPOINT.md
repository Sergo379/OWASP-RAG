# Stage 4 · checkpoint разработки

Фактическое состояние фиксируется в `experiments/runs/<RUN_ID>/state.json`. Оно может быть новее этого файла.

- Исходный RAG на `main` сохраняет вручную проверенные изменения Stage 3. `eval-check` сверил 55 активных документов и 4 опубликованных коллекции, всего 10 561 вектор.
- Набор `owasp-v1` заморожен отдельным commit `da2781c`. SHA-256 `fa61317c559ed9192095e503abeda0f4c6096df5695608d4d0fd4a1a075d3610`. Размечены 32 ответимых вопроса и 8 вопросов вне снимка. Пороговые и модельные параметры выбираются только на development.
- Retrieval `146eb445217746c3b693edabdb51d6b3` завершил 200/200 задач. Threshold `0c58239054014b6a8fa20b6a3b3e8a06` завершил 80/80. Чекпоинты и исследовательские индексы следует оставить на диске.
- Проверены `pixi run eval-check`, `pixi run lint`, 245 тестов и Stage 1 smoke. В браузере открылись реальные результаты поиска и абзац Secure в Question Inspector.
- Development generation `acaaa3811cda402aafda5cc192af2d6b` завершил 13/13 задач с двумя сохранёнными timeout 4B Instruct.
- Выбор `00c35cd42e6054c6` сохранён в `experiments/manifests/selection.json` до final. Следующая команда `pixi run eval-run --suite final --offline`. После создания run сохраняйте и используйте его ID для Resume.
