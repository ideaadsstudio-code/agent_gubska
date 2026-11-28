"""Ядро агента: Claude Messages API с tool use (ручной цикл).

Важно про историю: модели Claude 5.5 привязывают блоки thinking к префиксу диалога.
Поэтому история только дописывается: system и tools фиксированы, прошлые сообщения не меняются,
а события между ходами (ответ менеджера, правка черновика) передаются заметками в новом ходе клиента.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from .config import Settings
from .pricing import cost_usd
from .tools import TOOLS

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
# Модели, для которых сервер умеет повторить отказанный запрос на другой модели.
MODELS_WITH_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


class AgentError(Exception):
    """Не удалось получить ответ: ошибка API, таймаут, отказ модели или слишком длинный цикл."""

    def __init__(self, message: str):
        super().__init__(message)
        self.cost_usd = 0.0
        self.usage: dict[str, int] = {}
        self.tool_calls: list[ToolCall] = []


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass
class AgentResult:
    reply_text: str
    new_messages: list[dict[str, Any]]
    tool_calls: list[ToolCall] = field(default_factory=list)
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


def load_system_prompt(settings: Settings) -> str:
    with open(settings.system_prompt_path, encoding="utf-8") as f:
        prompt = f.read()
    with open(settings.knowledge_base_path, encoding="utf-8") as f:
        kb = f.read()
    return prompt.replace("{{KNOWLEDGE_BASE}}", kb).replace("{{STUDIO_NAME}}", settings.studio_name)


class Agent:
    def __init__(self, settings: Settings, client: Any, system_prompt: str):
        self.settings = settings
        self.client = client
        # Системный промпт читается один раз: любое изменение байтов сбрасывает кэш.
        self.set_system(system_prompt)

    def set_system(self, system_prompt: str) -> None:
        """Меняется только при запуске и когда менеджер принимает выученные правила (редко)."""
        self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    def build_request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        model = self.settings.claude_model
        betas = [THINKING_BINDING_BETA]
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self.settings.claude_max_tokens,
            "system": self.system,
            "tools": TOOLS,
            "messages": messages,
            # Автокэш последнего блока: история диалога тоже читается из кэша.
            "cache_control": {"type": "ephemeral"},
            # Thinking не отключается на Opus 5.5; глубину регулирует effort.
            # drop_block: если историю пришлось отредактировать (удаление сообщения по запросу Meta),
            # устаревшие блоки thinking отбрасываются вместо ошибки 400.
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            "output_config": {"effort": self.settings.claude_effort},
        }
        if model in MODELS_WITH_FALLBACK:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        params["betas"] = betas
        return params

    async def respond(
        self,
        history: list[dict[str, Any]],
        user_content: list[dict[str, Any]],
        execute_tool: ToolExecutor,
    ) -> AgentResult:
        new_messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        tool_calls: list[ToolCall] = []
        totals: dict[str, Any] = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0}
        try:
            return await self._loop(history, new_messages, tool_calls, totals, execute_tool)
        except AgentError as e:
            e.tool_calls = tool_calls
            e.usage = totals
            e.cost_usd = totals.get("cost", 0.0)
            raise

    async def _loop(self, history, new_messages, tool_calls, totals, execute_tool) -> AgentResult:
        total_cost = 0.0
        served_model = self.settings.claude_model
        for _ in range(self.settings.max_tool_iterations):
            params = self.build_request(history + new_messages)
            try:
                response = await self.client.with_options(
                    timeout=self.settings.claude_timeout_seconds
                ).beta.messages.create(**params)
            except anthropic.APITimeoutError as e:
                raise AgentError(f"Claude API timeout: {e}") from e
            except anthropic.RateLimitError as e:
                raise AgentError(f"Claude API rate limit: {e}") from e
            except anthropic.APIStatusError as e:
                raise AgentError(f"Claude API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise AgentError(f"Claude API connection error: {e}") from e

            served_model = getattr(response, "model", served_model) or served_model
            usage = response.usage
            u_in = usage.input_tokens or 0
            u_out = usage.output_tokens or 0
            u_cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
            u_cr = getattr(usage, "cache_read_input_tokens", 0) or 0
            totals["input"] += u_in
            totals["output"] += u_out
            totals["cache_write"] += u_cw
            totals["cache_read"] += u_cr
            total_cost += cost_usd(served_model, u_in, u_out, u_cw, u_cr)
            totals["cost"] = total_cost

            transforms = getattr(response, "input_transformations", None)
            if transforms:
                log.warning("Claude dropped thinking blocks: %s", transforms)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise AgentError(f"Claude refused (category={getattr(details, 'category', None)})")

            content = [block.to_dict() for block in response.content]
            new_messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    call = ToolCall(name=block.name, input=dict(block.input))
                    tool_calls.append(call)
                    try:
                        result = await execute_tool(call.name, call.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    except Exception as e:  # ошибка инструмента возвращается модели, а не роняет цикл
                        log.exception("tool %s failed", call.name)
                        results.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {e}", "is_error": True}
                        )
                new_messages.append({"role": "user", "content": results})
                continue

            text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens" and not text:
                raise AgentError("Claude hit max_tokens without a reply")
            return AgentResult(
                reply_text=text,
                new_messages=new_messages,
                tool_calls=tool_calls,
                cost_usd=total_cost,
                usage=totals,
                model=served_model,
            )

        raise AgentError(f"Tool loop exceeded {self.settings.max_tool_iterations} iterations")
