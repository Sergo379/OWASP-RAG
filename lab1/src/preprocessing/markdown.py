"""Conservative cleanup: fenced and indented code remains literal external data."""

import re
from dataclasses import dataclass


@dataclass
class Section:
    text: str
    heading_path: list[str]


def clean_markdown(raw: str) -> tuple[str, list[Section]]:
    raw = raw.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if raw.startswith("---\n"):
        raw = re.sub(r"\A---\n.*?\n---(?:\n|$)", "", raw, count=1, flags=re.S)
    lines, fence, comment = [], None, False
    for line in raw.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            lines.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            lines.append(line)
            continue
        if line.startswith(("    ", "\t")):
            lines.append(line)
            continue
        inline_code = []

        def protect(match):
            inline_code.append(match[0])
            return f"\x00CODE{len(inline_code) - 1}\x00"

        line = re.sub(r"(`+)(.*?)(?<!`)\1(?!`)", protect, line)
        # Strip multiline HTML comments outside code, retaining surrounding prose.
        output = ""
        while line:
            if comment:
                end = line.find("-->")
                if end < 0:
                    line = ""
                else:
                    line, comment = line[end + 3 :], False
            else:
                start = line.find("<!--")
                if start < 0:
                    output += line
                    line = ""
                else:
                    output += line[:start]
                    line, comment = line[start + 4 :], True
        line = re.sub(r"!\[([^\]]*)\]\([^\n)]*\)", r"\1", output).rstrip()
        for index, code in enumerate(inline_code):
            line = line.replace(f"\x00CODE{index}\x00", code)
        if line or (lines and lines[-1] != ""):
            lines.append(line)
    cleaned = "\n".join(lines).strip()
    sections, buffer, headings, fence = [], [], [], None
    for line in cleaned.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if not fence else None
        if heading:
            if buffer:
                sections.append(Section("\n".join(buffer).strip(), [h[1] for h in headings]))
            buffer = []
            level, title = len(heading[1]), heading[2]
            headings = [h for h in headings if h[0] < level] + [(level, title)]
        buffer.append(line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
    if buffer:
        sections.append(Section("\n".join(buffer).strip(), [h[1] for h in headings]))
    return cleaned, [s for s in sections if s.text]
