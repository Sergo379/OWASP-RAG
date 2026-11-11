"""Small, self-contained SVG figures generated from persisted measurements."""

from html import escape

from src.storage.files import atomic_write

COLORS = ("#3155a4", "#1d846a", "#b36822", "#7551a5")


def svg_text(x, y, label, size=13, anchor="start"):
    return f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}">{escape(str(label))}</text>'


def bar_chart(path, title, subtitle, axis, values, series, upper=None):
    if not values or not any(any(value is not None for value in row) for _, row in values):
        return None
    width, left, right = 930, 230, 855
    height = max(205, 125 + len(values) * (max(28, len(series) * 14 + 8)))
    row_height = (height - 125) / len(values)
    limit = upper or max(value for _, row in values for value in row if value is not None) or 1
    limit *= 1 if upper else 1.08
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">',
        f"<title>{escape(title)}</title><desc>{escape(subtitle)}</desc>",
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        '<g font-family="Segoe UI, Arial, sans-serif" fill="#182b49">',
        svg_text(24, 30, title, 19),
        svg_text(24, 52, subtitle, 12),
    ]
    for i in range(6):
        value = limit * i / 5
        x = left + (right - left) * i / 5
        elements.extend(
            [
                f'<path d="M{x:.1f},72 V{height - 53}" stroke="#dce3ed"/>',
                svg_text(x, height - 36, f"{value:.2g}", 12, "middle"),
            ]
        )
    for index, (label, row) in enumerate(values):
        top = 75 + index * row_height
        elements.append(svg_text(left - 10, top + row_height / 2, label, 12, "end"))
        for i, value in enumerate(row):
            if value is None:
                continue
            pixels = (right - left) * value / limit
            y = top + 14 * i
            elements.append(
                f'<rect x="{left}" y="{y:.1f}" width="{pixels:.1f}" height="11" fill="{COLORS[i]}"/>'
            )
            elements.append(svg_text(min(left + pixels + 5, right + 5), y + 10, f"{value:.3g}", 11))
    elements.append(svg_text((left + right) / 2, height - 16, axis, 12, "middle"))
    for i, name in enumerate(series):
        x = 24 + i * 175
        elements.extend(
            [
                f'<rect x="{x}" y="{height - 15}" width="9" height="9" fill="{COLORS[i]}"/>',
                svg_text(x + 12, height - 6, name, 11),
            ]
        )
    elements.append("</g></svg>")
    atomic_write(path, "\n".join(elements).encode())
    return path.name


def generate_charts(folder, summary, manifest):
    split = manifest["split"]
    configs = summary["configurations"]
    output = []

    def chart(name, title, unit, labels, fields, legends, upper=None, scale=1, notes=""):
        labels = [label for label in labels if label in configs]
        if not labels:
            return
        data = []
        for label in labels:
            record = configs[label]
            values = []
            for metric, statistic in fields:
                value = record["metrics"].get(metric, {}).get(statistic)
                values.append(value / scale if value is not None else None)
            data.append((f"{label} (n={record['n']})", values))
        file = bar_chart(
            folder / (name + ".svg"),
            title,
            f"{split}; n — сохранённые запросы на вариант. {notes}",
            unit,
            data,
            legends,
            upper,
        )
        if file:
            output.append(file)

    for metric, title in (("recall", "Recall@K"), ("mrr", "MRR@K")):
        chart(
            metric + "-topk",
            f"{title} по E5 и MiniLM",
            "Доля, 0–1",
            [f"topk-{model}-{k}" for model in ("e5", "minilm") for k in (3, 5, 10, 20)],
            [(metric, "mean")],
            [title],
            1,
            notes="Section 256, pool 80; среднее по вопросам с ответом.",
        )
    for family, title in (
        ("chunking", "Стратегия разбиения"),
        ("size", "Размер chunk"),
        ("overlap", "Перекрытие chunks"),
        ("reranking", "Cross-encoder reranking"),
        ("threshold", "Порог cosine similarity"),
    ):
        chart(
            family,
            title,
            "Доля, 0–1",
            [name for name, info in configs.items() if info["family"] == family],
            [("recall", "mean"), ("mrr", "mean"), ("empty_context", "mean")],
            ["Recall", "MRR", "Пустой контекст"],
            1,
        )
    gen = [name for name, info in configs.items() if info["family"] in ("llm", "prompt", "final_generation")]
    chart(
        "llm-quality",
        "Результаты генерации",
        "Доля, 0–1",
        gen,
        [("citation_validity", "mean"), ("refusal_accuracy", "mean")],
        ["Citation ID validity", "Refusal accuracy"],
        1,
        notes="Автоматические показатели. Смысловые баллы доступны после разметки.",
    )
    chart(
        "refusal",
        "Правильность решения об отказе",
        "Доля, 0–1",
        gen,
        [("refusal_accuracy", "mean")],
        ["Refusal accuracy"],
        1,
        notes="Отказы на ответимых вопросах считаются ошибками.",
    )
    reviewed = [
        (
            f"{name} (n={configs[name]['reviewed']})",
            [
                configs[name]["review_metrics"].get(key, {}).get("mean")
                for key in (
                    "correctness",
                    "faithfulness",
                    "completeness",
                    "citation_support",
                )
            ],
        )
        for name in gen
        if configs[name]["reviewed"]
    ]
    manual_figure = bar_chart(
        folder / "reviewed-quality.svg",
        "Ручная оценка ответов",
        f"{split}. n — ответы с оценкой.",
        "Баллы, 0–2",
        reviewed,
        ["Correctness", "Faithfulness", "Completeness", "Citation support"],
        2,
    )
    if manual_figure:
        output.append(manual_figure)
    labels = list(configs)
    chart(
        "latency",
        "Задержка запросов",
        "мс",
        labels,
        [("latency_ms", "median"), ("latency_ms", "p95")],
        ["Медиана", "p95"],
        notes="Холодные загрузки включены в распределение.",
    )
    chart(
        "ram",
        "Пиковая память процесса",
        "MiB",
        labels,
        [("peak_python_rss_bytes", "max"), ("peak_combined_rss_bytes", "max")],
        ["Python", "Python + Ollama"],
        scale=1048576,
    )
    chart(
        "overview",
        "Сводка конфигураций поиска",
        "Доля, 0–1",
        labels,
        [("recall", "mean"), ("mrr", "mean"), ("hit_rate", "mean")],
        ["Recall", "MRR", "Hit Rate"],
        1,
    )
    return output
