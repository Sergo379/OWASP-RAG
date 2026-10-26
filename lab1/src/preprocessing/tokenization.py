"""Only the E5 tokenizer JSON is downloaded; no model weights or torch."""

from pathlib import Path

import httpx
from tokenizers import Encoding, Tokenizer

from src.core.config import AppError
from src.core.models import TokenizerConfig
from src.storage.files import atomic_write, digest


class E5Tokenizer:
    def __init__(self, config: TokenizerConfig, cache: Path, client: httpx.Client | None = None):
        self.identity = f"{config.model}@{config.revision}"
        path = cache / f"{config.revision}.json"
        if not path.exists():
            own = client is None
            client = client or httpx.Client(timeout=60, follow_redirects=True)
            try:
                with client.stream(
                    "GET", f"https://huggingface.co/{config.model}/resolve/{config.revision}/tokenizer.json"
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > 30_000_000:
                            raise AppError("Файл токенизатора превышает лимит 30 MB")
                self._validate(bytes(data), config.sha256)
                atomic_write(path, bytes(data))
            except httpx.HTTPError as exc:
                raise AppError(
                    "Не удалось скачать токенизатор E5. Проверьте сеть и выполните pixi run tokenizer"
                ) from exc
            finally:
                if own:
                    client.close()
        try:
            data = path.read_bytes()
            self._validate(data, config.sha256)
            self.backend = Tokenizer.from_str(data.decode("utf-8"))
            self.backend.no_truncation()
            self.backend.no_padding()
        except (OSError, ValueError) as exc:
            raise AppError(
                "Повреждён кеш токенизатора; удалите указанный файл кеша и повторите pixi run tokenizer"
            ) from exc

    @staticmethod
    def _validate(data, expected):
        if expected and digest(data) != expected:
            raise AppError("SHA256 токенизатора не совпал")
        try:
            Tokenizer.from_str(data.decode("utf-8"))
        except Exception as exc:
            raise AppError("Некорректный tokenizer.json") from exc

    def encode(self, text: str) -> Encoding:
        return self.backend.encode(text, add_special_tokens=False)

    def count(self, text: str) -> int:
        return len(self.encode(text).ids)
