"""Bounded Ollama NDJSON transport. Closing the response cancels upstream work."""

import asyncio
import json
import threading
import time

import httpx

from src.core.config import AppError


class Cancelled(AppError):
    pass


class GenerationError(AppError):
    def __init__(self, message, metrics):
        super().__init__(message)
        self.metrics = metrics


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.lock = threading.Lock()
        self.task = self.loop = None

    def check(self):
        if self.event.is_set():
            raise Cancelled("Генерация остановлена пользователем")

    def attach(self, response):
        with self.lock:
            self.response = response
            if self.event.is_set():
                response.close()
        self.check()

    def cancel(self):
        self.event.set()
        with self.lock:
            if self.response is not None:
                self.response.close()
            if self.task is not None and self.loop is not None:
                try:
                    self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:
                    pass  # The request event loop has already finished.


class ThinkingFilter:
    """Incremental filter; retains possible split tags instead of leaking them."""

    def __init__(self):
        self.buffer, self.hidden = "", False

    def feed(self, text, final=False):
        self.buffer += text
        output = ""
        while self.buffer:
            tag = "</think>" if self.hidden else "<think>"
            found = self.buffer.lower().find(tag)
            if found >= 0:
                if not self.hidden:
                    output += self.buffer[:found]
                self.buffer = self.buffer[found + len(tag) :]
                self.hidden = not self.hidden
                continue
            # Hold only a suffix that could be an incomplete tag.
            hold = next(
                (
                    n
                    for n in range(min(len(tag) - 1, len(self.buffer)), 0, -1)
                    if self.buffer.lower().endswith(tag[:n])
                ),
                0,
            )
            end = len(self.buffer) - hold
            if not self.hidden:
                output += self.buffer[:end]
            self.buffer = self.buffer[end:]
            if final:
                self.buffer = ""  # Never release unfinished tags or unfinished thinking.
            break
        return output


class Ollama:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    def client(self, timeout=None):
        return httpx.Client(
            base_url=self.config.base_url, timeout=timeout or 3, transport=self.transport, trust_env=False
        )

    def status(self):
        try:
            with self.client() as client:
                version = client.get("/api/version")
                version.raise_for_status()
                tags = client.get("/api/tags")
                tags.raise_for_status()
                installed = {m["name"]: m for m in tags.json()["models"]}
            return {
                "available": True,
                "version": version.json()["version"],
                "models": [
                    {
                        "name": key,
                        "digest": installed.get(key, {}).get("digest"),
                        "size_bytes": installed.get(key, {}).get("size"),
                        "ready": installed.get(key, {}).get("digest") == expected,
                        "expected_digest": expected,
                    }
                    for key, expected in self.config.models.items()
                ],
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {
                "available": False,
                "models": [],
                "error": "Ollama API недоступен. Запустите Ollama и выполните ollama pull qwen3:1.7b / qwen3:4b.",
            }

    def stream(self, model, messages, cancel, emit):
        return asyncio.run(self._stream(model, messages, cancel, emit))

    async def _stream(self, model, messages, cancel, emit):
        cancel.check()
        state = self.status()
        if not state["available"]:
            raise AppError(state["error"])
        if not any(m["name"] == model and m["ready"] for m in state["models"]):
            raise AppError(
                "Модель Ollama отсутствует или digest изменён. Проверьте llm-check и generation.yaml"
            )
        config = self.config
        runtime_model = model
        effective_think = model in config.thinking_models
        max_predict = config.model_num_predict.get(model, config.num_predict)
        started, first = time.perf_counter(), None
        done, text, filt = None, "", ThinkingFilter()
        try:
            with self.client() as client:
                profile = client.post("/api/show", json={"model": runtime_model})
                profile.raise_for_status()
                template = profile.json()["template"]
                if "completion" not in profile.json().get("capabilities", []):
                    raise AppError("Ollama не подтвердил completion capability модели")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError("Не удалось проверить template/capabilities Ollama") from exc
        # Some pinned Qwen3 tags have a legacy template that pre-opens <think>
        # even when think=false. Use the structured thinking channel for this tag.
        legacy_thinking_prefix = (
            "thinking" in profile.json().get("capabilities", [])
            and "$.Think" not in template
            and "<think>" in template
        )
        variant = profile.json().get("model_info", {}).get("general.finetune")
        if legacy_thinking_prefix and not effective_think and variant != "Instruct":
            raise AppError(
                "Legacy template не соблюдает think=false. Включите qwen3:4b в thinking_models; thinking channel будет отброшен"
            )
        filt.hidden = effective_think
        structured_thinking_seen = False
        messages = [dict(m) for m in messages]
        for message in reversed(messages):
            if message["role"] == "user":
                if not message["content"].rstrip().endswith("/no_think"):
                    message["content"] += "\n/no_think"
                break
        body = {
            "model": runtime_model,
            "messages": messages,
            "stream": True,
            "think": effective_think,
            "keep_alive": config.keep_alive,
            "options": {
                "temperature": config.model_temperatures.get(model, config.temperature),
                "num_ctx": config.num_ctx,
                "num_predict": max_predict,
                "num_thread": config.num_thread,
                "num_gpu": 0,
                "seed": 42,
            },
        }
        if "thinking" not in profile.json().get("capabilities", []):
            body.pop("think")
        try:
            with cancel.lock:
                cancel.loop, cancel.task = asyncio.get_running_loop(), asyncio.current_task()
            cancel.check()
            async with (
                asyncio.timeout(config.timeout_seconds),
                httpx.AsyncClient(
                    base_url=config.base_url,
                    timeout=httpx.Timeout(config.timeout_seconds, connect=3),
                    transport=self.transport,
                    trust_env=False,
                ) as client,
            ):
                async with client.stream("POST", "/api/chat", json=body) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        cancel.check()
                        if time.perf_counter() - started > config.timeout_seconds:
                            raise AppError("Превышен общий timeout генерации")
                        if not line:
                            continue
                        if len(line) > 1_000_000:
                            raise AppError("Некорректный размер события Ollama")
                        event = json.loads(line)
                        if event.get("error"):
                            raise AppError(
                                "Ollama сообщил об ошибке генерации. Проверьте память и состояние модели"
                            )
                        # Ignore the structured thinking field. On a legacy server
                        # that returns it inline instead, the initially hidden filter
                        # suppresses everything through the closing thinking tag.
                        if event.get("message", {}).get("thinking") and not structured_thinking_seen:
                            structured_thinking_seen = True
                            filt = ThinkingFilter()
                        part = filt.feed(event.get("message", {}).get("content", ""))
                        if part:
                            first = first or time.perf_counter()
                            text += part
                            if len(text) > 65536:
                                raise AppError("Превышен лимит ответа")
                            emit(part)
                        if event.get("done"):
                            done = event
                            break
                    tail = filt.feed("", final=True)
                    if tail:
                        text += tail
                        emit(tail)
        except asyncio.CancelledError as exc:
            cancel.check()
            raise Cancelled("Генерация отменена; upstream соединение закрыто") from exc
        except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
            cancel.check()
            raise AppError(
                "Соединение Ollama прервано или истёк timeout. Частичный ответ не считается завершённым"
            ) from exc
        finally:
            with cancel.lock:
                cancel.response = None
                cancel.task = cancel.loop = None
        cancel.check()
        if not done:
            raise AppError("Ollama завершил поток без done; ответ неполный")
        metrics = {
            k: done.get(k)
            for k in (
                "done_reason",
                "prompt_eval_count",
                "eval_count",
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
            )
        }
        metrics.update(
            ttft_ms=round((first - started) * 1000, 2) if first else None,
            wall_ms=round((time.perf_counter() - started) * 1000, 2),
            think_requested=effective_think,
            think_supported="thinking" in profile.json().get("capabilities", []),
            structured_thinking_discarded=structured_thinking_seen,
            max_output_tokens=max_predict,
            model_variant=profile.json().get("model_info", {}).get("general.finetune"),
            runtime_model=runtime_model,
            runtime_digest=config.models[model],
        )
        if done.get("done_reason") == "length":
            raise GenerationError(
                "Достигнут num_predict: ответ неполный. Сузьте вопрос или выберите concise_grounded", metrics
            )
        return text.strip(), metrics
