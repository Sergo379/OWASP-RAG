from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from src.core.config import AppError, load_yaml
from src.core.models import StrictModel, TokenizerConfig
from src.retrieval.models import SearchRequest


class ModeBudget(StrictModel):
    num_ctx: int = Field(4096, ge=2048, le=8192)
    num_predict: int = Field(896, ge=64, le=2048)
    context_tokens: int = Field(2400, ge=128, le=6000)


class GenerationConfig(StrictModel):
    schema_version: Literal[1] = 1
    base_url: str = "http://127.0.0.1:11434"
    default_model: Literal["qwen3:1.7b", "qwen3:4b", "qwen3:4b-instruct-2507-q4_K_M"] = "qwen3:1.7b"
    models: dict[str, str]
    temperature: float = Field(0.1, ge=0, le=1)
    num_ctx: int = Field(4096, ge=2048, le=8192)
    num_predict: int = Field(512, ge=32, le=2048)
    model_num_predict: dict[str, int] = {"qwen3:4b": 2048}
    model_temperatures: dict[str, float] = {"qwen3:4b": 0.6}
    num_thread: int = Field(4, ge=1, le=8)
    timeout_seconds: int = Field(360, ge=10, le=600)
    keep_alive: Literal[0] = 0
    think: Literal[False] = False
    thinking_models: list[Literal["qwen3:4b"]] = ["qwen3:4b"]
    context_tokens: int = Field(2400, ge=128, le=6000)
    history_tokens: int = Field(400, ge=0, le=1000)
    history_turns: int = Field(2, ge=0, le=4)
    template_reserve_tokens: int = Field(192, ge=128, le=512)
    max_messages: int = Field(40, ge=2, le=100)
    unload_retrieval: bool = True
    tokenizer: TokenizerConfig
    mode_budgets: dict[str, ModeBudget] = Field(
        default_factory=lambda: {
            "concise_grounded": ModeBudget(num_predict=512),
            "strict_grounded": ModeBudget(num_predict=896),
            "technical_grounded": ModeBudget(num_ctx=6144, num_predict=1536, context_tokens=3600),
        }
    )
    default_retrieval_strategy: Literal["baseline", "hybrid_context"] = "hybrid_context"
    repair_on_validation_failure: bool = True
    default_answer_format: Literal["prose", "evidence_bound"] = "evidence_bound"
    repair_feedback_tokens: int = Field(384, ge=128, le=512)
    chat_document_focus: bool = True

    def preferred_answer_format(self, mode, model):
        # Preserve the concise and pinned legacy Thinking generation paths.
        return (
            "prose"
            if mode == "concise_grounded" or model in self.thinking_models
            else self.default_answer_format
        )

    @model_validator(mode="after")
    def safe_limits(self):
        url = urlparse(self.base_url)
        if (
            url.scheme != "http"
            or url.hostname not in ("127.0.0.1", "localhost")
            or url.path
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Ollama must be a loopback HTTP origin")
        if (
            not {"qwen3:1.7b", "qwen3:4b"}.issubset(self.models)
            or set(self.models) - {"qwen3:1.7b", "qwen3:4b", "qwen3:4b-instruct-2507-q4_K_M"}
        ) or any(len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in self.models.values()):
            raise ValueError("Both Qwen tags require pinned SHA256 digests")
        if set(self.model_num_predict) - set(self.models) or any(
            not 32 <= n <= 2048 for n in self.model_num_predict.values()
        ):
            raise ValueError("Invalid per-model output budget")
        if (
            256 + max([self.num_predict, *self.model_num_predict.values()]) + self.template_reserve_tokens
            >= self.num_ctx
        ):
            raise ValueError("Context/output exceed num_ctx")
        if set(self.model_temperatures) - set(self.models) or any(
            not 0 <= n <= 1 for n in self.model_temperatures.values()
        ):
            raise ValueError("Invalid per-model temperature")
        if set(self.mode_budgets) != {"concise_grounded", "strict_grounded", "technical_grounded"}:
            raise ValueError("Three mode budgets required")
        for budget in self.mode_budgets.values():
            if budget.num_predict + self.template_reserve_tokens + 256 >= budget.num_ctx:
                raise ValueError("Mode has no input budget")
        return self

    def for_request(self, mode, model, query):
        from src.generation.context import brief_requested

        effective_mode = "concise_grounded" if brief_requested(query) else mode
        budget = self.mode_budgets[effective_mode]
        return self.model_copy(
            update={
                **budget.model_dump(),
                "num_predict": max(budget.num_predict, self.model_num_predict.get(model, 0)),
                "temperature": self.model_temperatures.get(model, self.temperature),
            }
        )


class ChatRequest(StrictModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retrieval: SearchRequest
    llm: Literal["qwen3:1.7b", "qwen3:4b", "qwen3:4b-instruct-2507-q4_K_M"] = "qwen3:1.7b"
    prompt_mode: Literal["strict_grounded", "concise_grounded", "technical_grounded"] = "strict_grounded"
    retrieval_strategy: Literal["baseline", "hybrid_context"] = "baseline"
    answer_format: Literal["prose", "evidence_bound"] = "prose"


def load_generation(root):
    try:
        config = GenerationConfig.model_validate(load_yaml(root / "configs/generation.yaml"))
        prompts = load_yaml(root / "configs/prompts.yaml")
        if not all(
            isinstance(prompts.get(k), str) and prompts[k].strip()
            for k in ("version", "strict_grounded", "concise_grounded", "technical_grounded")
        ):
            raise ValueError("Missing grounded prompt")
        return config, prompts
    except ValueError as exc:
        raise AppError("Некорректная конфигурация generation/prompts") from exc
