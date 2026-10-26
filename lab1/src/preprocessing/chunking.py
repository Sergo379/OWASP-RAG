"""Shared bounded splitter. Counts are rechecked on the actual stored substrings."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from src.core.config import AppError
from src.core.models import PreprocessingConfig
from src.preprocessing.markdown import Section
from src.preprocessing.tokenization import E5Tokenizer


@dataclass
class Piece:
    text: str
    heading_path: list[str]


def windows(text: str, tokenizer: E5Tokenizer, size: int, overlap: float = 0) -> Iterator[str]:
    rest = text
    while rest.strip():
        encoding = tokenizer.encode(rest)
        if len(encoding.ids) <= size:
            if encoding.ids:
                yield rest
            return
        end = encoding.offsets[size][0]
        if end <= 0:
            end = encoding.offsets[size - 1][1]
        # A substring can retokenize differently at its end. Reduce without truncation.
        while end > 0 and tokenizer.count(rest[:end]) > size:
            end -= 1
        if end <= 0:
            raise AppError("Невозможно разбить текст в пределах token budget")
        part = rest[:end]
        if part.strip() and tokenizer.count(part):
            yield part
        advance = end
        if overlap:
            offsets = tokenizer.encode(rest[:end]).offsets
            amount = min(int(size * overlap), max(len(offsets) - 1, 0))
            if amount:
                advance = max(1, offsets[-amount][0])
        rest = rest[advance:]


def paragraphs(text: str) -> list[str]:
    """Blank lines inside a fenced code block are not paragraph boundaries."""
    blocks, lines, fence = [], [], None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not line.strip() and fence is None:
            if lines:
                blocks.append("\n".join(lines))
                lines = []
        else:
            lines.append(line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
    if lines:
        blocks.append("\n".join(lines))
    return blocks


def pack(blocks: list[str], tokenizer: E5Tokenizer, size: int) -> Iterator[str]:
    pending = ""
    for block in blocks:
        candidate = f"{pending}\n\n{block}".strip()
        if tokenizer.count(candidate) <= size:
            pending = candidate
        else:
            if pending:
                yield pending
                pending = ""
            parts = list(windows(block, tokenizer, size))
            yield from parts[:-1]
            pending = parts[-1] if parts else ""
    if pending:
        yield pending


def split(
    cleaned: str, sections: list[Section], config: PreprocessingConfig, tokenizer: E5Tokenizer
) -> Iterator[Piece]:
    if config.strategy in ("fixed", "overlap"):
        # Track the heading at each chunk's starting position, including overlapping windows.
        boundaries, offset = [], 0
        for section in sections:
            start = cleaned.find(section.text, offset)
            boundaries.append((start, section.heading_path))
            offset = start + len(section.text)
        cursor = 0
        for part in windows(cleaned, tokenizer, config.chunk_size, config.overlap):
            start = cleaned.find(part, cursor)
            if start < 0:
                raise AppError("Ошибка определения позиции chunk")
            path = next((p for pos, p in reversed(boundaries) if pos <= start), [])
            yield Piece(part, path)
            cursor = start + 1
    else:
        for section in sections:
            blocks = paragraphs(section.text) if config.strategy == "paragraph" else [section.text]
            for part in pack(blocks, tokenizer, config.chunk_size):
                yield Piece(part, section.heading_path)
