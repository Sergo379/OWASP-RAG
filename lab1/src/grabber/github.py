"""Two discovery calls per snapshot; raw downloads are pinned to the same commit."""

import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from src.core.config import AppError
from src.core.models import CorpusConfig, Document
from src.storage.files import Store, atomic_write, digest, now

log = logging.getLogger("owasp.grabber")


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class GitHubGrabber:
    def __init__(
        self,
        config: CorpusConfig,
        store: Store,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config, self.store = config, store
        self.client = client or httpx.Client(timeout=config.timeout, follow_redirects=True)
        self.owns_client = client is None
        self.sleep = sleep

    def request(self, url: str) -> bytes:
        headers = {"User-Agent": "OWASP-RAG/0.1", "Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token and url.startswith("https://api.github.com/"):
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(self.config.retries + 1):
            try:
                with self.client.stream("GET", url, headers=headers) as response:
                    retryable = response.status_code in (429, 500, 502, 503, 504) or (
                        response.status_code == 403
                        and (
                            response.headers.get("x-ratelimit-remaining") == "0"
                            or "retry-after" in response.headers
                        )
                    )
                    if retryable:
                        if attempt == self.config.retries:
                            raise AppError(
                                f"GitHub HTTP {response.status_code}: лимит запросов или временная ошибка; повторите позже"
                            )
                        try:
                            wait = float(response.headers.get("retry-after", 2**attempt))
                            if response.headers.get("x-ratelimit-remaining") == "0":
                                wait = max(
                                    wait, float(response.headers.get("x-ratelimit-reset", 0)) - time.time()
                                )
                        except ValueError:
                            wait = 2**attempt
                        # Never retry before a server's long rate-limit window expires.
                        if wait > self.config.max_backoff and response.status_code in (403, 429):
                            raise AppError(
                                f"GitHub HTTP {response.status_code}: rate limit; повторите после сброса лимита"
                            )
                        self.sleep(min(max(wait, 0), self.config.max_backoff))
                        continue
                    if response.status_code != 200:
                        raise AppError(
                            f"GitHub HTTP {response.status_code}; проверьте источник и доступ к сети"
                        )
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > max(self.config.max_document_bytes, 5_000_000):
                            raise AppError("Ответ GitHub превышает допустимый размер")
                    return bytes(data)
            except httpx.RequestError as exc:
                if attempt == self.config.retries:
                    raise AppError("GitHub недоступен: timeout или ошибка сети") from exc
                self.sleep(min(2**attempt, self.config.max_backoff))
        raise AppError("Не удалось получить ответ GitHub")

    def json(self, url):
        import json

        try:
            return json.loads(self.request(url))
        except (ValueError, UnicodeError) as exc:
            raise AppError("Повреждённый JSON-ответ GitHub") from exc

    def discover(self) -> tuple[str, dict[str, str]]:
        base = f"https://api.github.com/repos/{self.config.repository}"
        try:
            commit = self.json(f"{base}/commits/{quote(self.config.ref, safe='')}")["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                raise ValueError()
            tree = self.json(f"{base}/git/trees/{commit}?recursive=1")
            if tree.get("truncated") is not False:
                raise ValueError()
            files = {item["path"]: item["sha"] for item in tree["tree"] if item["type"] == "blob"}
            if any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in files.values()):
                raise ValueError()
            return commit, files
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise AppError("Некорректное или неполное дерево GitHub") from exc

    def run(self, progress: Callable[..., None] = lambda **kw: None) -> dict:
        try:
            manifest = self.store.manifest()  # Validate before any network access or mutation.
            commit, files = self.discover()
            result = {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0, "errors": []}
            selected = {item.path for item in self.config.documents}
            for doc in manifest.documents.values():
                if doc.source_path not in selected:
                    doc.status = "out_of_scope"
            for index, item in enumerate(self.config.documents):
                doc_id = digest(f"{self.config.repository}:{item.path}".encode())
                old = manifest.documents.get(doc_id)
                try:
                    if item.path not in files:
                        if old:
                            old.status, old.last_checked_at = "deleted", now()
                        result["deleted"] += 1
                        result["errors"].append(
                            {"path": item.path, "message": "Документ отсутствует в источнике"}
                        )
                    else:
                        unchanged = old is not None and old.git_blob_sha == files[item.path]
                        if unchanged:
                            try:
                                self.store.raw(old.content_sha256)
                            except AppError:
                                unchanged = False
                        if unchanged:
                            old.status, old.last_checked_at = "active", now()
                            # repository_commit remains the actual provenance of the stored bytes.
                            result["unchanged"] += 1
                        else:
                            data = self.request(
                                f"https://raw.githubusercontent.com/{self.config.repository}/{commit}/{item.path}"
                            )
                            if not data.strip() or len(data) > self.config.max_document_bytes:
                                raise AppError("Пустой или слишком большой документ")
                            if blob_sha(data) != files[item.path]:
                                raise AppError("Git blob SHA не совпал; документ не сохранён")
                            try:
                                text = data.decode("utf-8")
                            except UnicodeError as exc:
                                raise AppError("Документ не является UTF-8 Markdown") from exc
                            sha = digest(data)
                            atomic_write(self.store.raw_path(sha), data)
                            title = next(
                                (
                                    line.lstrip("# ").strip()
                                    for line in text.splitlines()
                                    if line.startswith("# ")
                                ),
                                item.path.split("/")[-1],
                            )
                            manifest.documents[doc_id] = Document(
                                document_id=doc_id,
                                source=self.config.repository,
                                source_path=item.path,
                                url=f"https://cheatsheetseries.owasp.org/{item.path[:-3]}.html",
                                title=title,
                                topic=item.topic,
                                git_blob_sha=files[item.path],
                                content_sha256=sha,
                                repository_commit=commit,
                                fetched_at=now(),
                                last_checked_at=now(),
                            )
                            result["changed" if old else "new"] += 1
                    manifest.repository_commit, manifest.last_updated_at = commit, now()
                    self.store.save_manifest(manifest)  # Durable checkpoint after every document.
                    log.info("Document checked: %s", item.path, extra={"operation": "grab"})
                except AppError as exc:
                    result["errors"].append({"path": item.path, "message": str(exc)})
                    log.warning("%s: %s", item.path, exc, extra={"operation": "grab"})
                progress(completed=index + 1, total=len(selected), current=item.path)
            return {**result, "source_commit": commit}
        finally:
            if self.owns_client:
                self.client.close()
