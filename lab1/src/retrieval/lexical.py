"""Small bilingual term normalizer and BM25 over existing published chunks.

Aliases are retrieval vocabulary, not recommendations or question-specific answers.
No new weights, embeddings, persistent index, or external service.
"""

import math
import re
from collections import Counter
from functools import lru_cache

ALIASES = {
    "transport": r"tls|https|ssl|transport|transmission|переда\w*|трафик\w*|интернет\w*",
    "security": r"secur(?:ity|ing)|защит\w*|безопасн\w*",
    "confidential": r"confidential\w*|конфиденц\w*",
    "data": r"data|данн\w*",
    "protocol": r"protocols?|versions?|протокол\w*|верси\w*",
    "cipher": r"ciphers?|шифр(?:ы|ов|ам|ами|ах)?",
    "certificate": r"certificat\w*|сертификат\w*",
    "verify": r"verif\w*|test\w*|check\w*|scan\w*|проверк\w*|проверя\w*",
    "config": r"configur\w*|конфигура\w*|настрой\w*",
    "cookie": r"cookies?|кук\w*",
    "encryption": r"encrypt\w*|шифров\w*|шифрованием",
    "hash": r"hash\w*|хеш\w*|хэш\w*",
    "encoding": r"encod\w*|кодиров\w*",
    "password": r"passwords?|парол\w*",
    "storage": r"stor(?:e|es|ing|age)|хран\w*",
    "salt": r"salt(?:s|ing)?|сол[ьи]\w*|соль",
    "algorithm": r"algorithms?|алгоритм\w*",
    "sql": r"sql|запрос\w*",
    "parameter": r"paramet\w*|prepared|binding|параметр\w*|конкатенац\w*",
    "privilege": r"privileges?|привилег\w*",
    "authentication": r"authenticat\w*|аутентификац\w*",
    "authorization": r"authoriz\w*|авторизац\w*",
    "object": r"objects?|idor|объект\w*|профил\w*",
    "access": r"access|доступ\w*|чуж\w*",
}
PATTERNS = [(key, re.compile("^(?:" + pattern + ")$", re.I)) for key, pattern in ALIASES.items()]


@lru_cache(maxsize=20000)
def normalize(term):
    for canonical, pattern in PATTERNS:
        if pattern.fullmatch(term):
            return canonical
    return term


def terms(text):
    text = re.sub(r"(?i)(?:HTTP\s+)?Strict\s+Transport\s+Security", "hsts", text)
    return [normalize(t) for t in re.findall(r"[a-zа-яё]+|\d+(?:\.\d+)?", text.lower()) if len(t) > 1]


def named_controls(query):
    return set(re.findall(r"\b(?:Secure|HttpOnly|SameSite|Base64|Argon2id|PBKDF2|bcrypt)\b", query, re.I))


class BM25:
    def __init__(self, rows):
        self.rows = rows
        self.bags = [
            Counter(terms(r["original_text"]) + terms(r["title"]) * 3 + terms(r["section"]) * 3) for r in rows
        ]

        self.lengths = [sum(b.values()) for b in self.bags]
        self.average = sum(self.lengths) / max(1, len(rows))
        freq = Counter(t for bag in self.bags for t in bag)
        self.idf = {t: math.log(1 + (len(rows) - n + 0.5) / (n + 0.5)) for t, n in freq.items()}

    def rank(self, query, limit):
        tokens = set(terms(query))
        scores = []
        for i, bag in enumerate(self.bags):
            denominator = 1.2 * (0.25 + 0.75 * self.lengths[i] / max(1, self.average))
            score = sum(self.idf.get(t, 0) * bag[t] * 2.2 / (bag[t] + denominator) for t in tokens if bag[t])
            if score:
                scores.append((score, self.rows[i]))
        return sorted(scores, key=lambda pair: (-pair[0], pair[1]["chunk_id"]))[:limit]
