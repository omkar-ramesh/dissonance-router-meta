"""Streaming chat client for the Meta Model API (OpenAI-compatible Chat Completions).

Every tier is a Muse model; what differs is the model id and `reasoning_effort`. The API streams
server-sent events, so we can measure time-to-first-token. Muse Spark always reasons: the lowest
setting is "minimal" ("none" is rejected), and reasoning tokens are billed as output.
"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src import config

# Some reasoning models inline their thinking in <think> tags; strip them if they appear.
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


class LLMError(RuntimeError):
    """The model backend was unreachable or returned an error."""


@dataclass
class LLMResult:
    text: str
    tier: str
    model: str
    latency_ms: int
    reasoning: str = ""
    usage: dict = field(default_factory=dict)
    ttft_ms: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    model_name: str = ""
    context_window: Optional[int] = None
    est_cost_usd: Optional[float] = None
    est_cost_if_ultra_usd: Optional[float] = None
    reasoning_on: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    reasoning_token_count: Optional[int] = None     # reported by the API when it says so
    cached_tokens: int = 0

    @property
    def reasoning_tokens(self) -> int:
        """Size of the thinking phase: as reported by the API, else ~4 chars per token. Billed as output."""
        if self.reasoning_token_count is not None:
            return self.reasoning_token_count
        return len(self.reasoning) // 4

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_sec(self) -> float:
        """Output tokens over wall-clock time (includes prompt processing and TTFT)."""
        return round(self.output_tokens / (self.latency_ms / 1000), 1) if self.latency_ms else 0.0

    @property
    def context_pct(self) -> Optional[float]:
        if not self.context_window:
            return None
        return round(100 * self.total_tokens / self.context_window, 1)

    def stats(self) -> dict:
        """Flat numbers for the dev dashboard and logs."""
        return {
            "tier": self.tier,
            "model_id": self.model,
            "model_name": self.model_name or self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reasoning_on": self.reasoning_on,
            "reasoning_effort": self.reasoning_effort,
            "cached_tokens": self.cached_tokens,
            "tokens_estimated": self.tokens_estimated,
            "tokens_per_sec": self.tokens_per_sec,
            "ttft_ms": self.ttft_ms,
            "latency_ms": self.latency_ms,
            "agent_rounds": 1,
            "context_window": self.context_window,
            "context_pct": self.context_pct,
            "est_cost_usd": self.est_cost_usd,
            "est_cost_if_ultra_usd": self.est_cost_if_ultra_usd,
        }


class LLMClient:
    """Calls the concrete Muse model behind a router tier ("nano" | "super" | "ultra")."""

    def __init__(
        self,
        base_url: str,
        models: dict,
        api_key: str = "",
        timeout: float = 180.0,
        transport: Optional[httpx.BaseTransport] = None,
        context_windows: Optional[dict] = None,
        prices: Optional[dict] = None,
        reasoning: Optional[dict] = None,
        max_tokens: Optional[dict] = None,
    ):
        """`context_windows` is {tier: tokens}. `prices` maps tier -> {"in", "out", "cached"} USD per 1M
        tokens, for cost estimates. `reasoning` ({tier: effort}) is the default reasoning_effort per tier;
        `max_tokens` ({tier: n}, 0 = none) caps output."""
        self.models = models
        self._context_windows = context_windows or {}
        self._prices = prices or {}
        self._reasoning = reasoning or {}
        self._max_tokens = max_tokens or {}
        self._http = self._make_http(base_url, api_key, timeout, transport)

    @staticmethod
    def _make_http(base_url: str, api_key: str, timeout: float, transport) -> httpx.Client:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    @classmethod
    def from_config(cls) -> "LLMClient":
        """Build a client for the models of the active profile in config."""
        if not config.META_API_KEY:
            raise LLMError("MODEL_API_KEY is not set in .env (get a key at https://dev.meta.ai)")
        return cls(
            config.META_BASE_URL, config.MODEL_TIERS, config.META_API_KEY,
            context_windows=config.CONTEXT_WINDOWS, prices=config.PRICES,
            reasoning=config.REASONING, max_tokens=config.MAX_TOKENS,
        )

    def effort_for(self, tier: str, reasoning: Optional[str] = None) -> str:
        """The reasoning_effort to send. `reasoning` may be an effort name, "off" (= minimal, the lowest
        the API allows) or "on" (= the tier's own effort, at least "medium")."""
        own = self._reasoning.get(tier) or "minimal"
        if reasoning in config.EFFORTS:
            return reasoning
        if reasoning == "off":
            return "minimal"
        if reasoning == "on":
            return own if own != "minimal" else "medium"
        return own

    def chat(
        self,
        tier: str,
        messages: list[dict],
        temperature: Optional[float] = None,
        reasoning: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """Stream a chat completion from the tier's model and return its cleaned answer + stats.

        `reasoning` (an effort, "on" or "off") and `max_tokens` override the tier's configured defaults.
        `temperature` is only sent when given: Meta recommends leaving it unset.
        """
        if tier not in self.models:
            raise ValueError(f"Unknown tier {tier!r}; expected one of {list(self.models)}")
        model = self.models[tier]
        effort = self.effort_for(tier, reasoning)

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": effort,
            "prompt_cache_key": f"dissonance-{tier}",
        }
        if temperature is not None:
            payload["temperature"] = temperature
        cap = max_tokens if max_tokens is not None else self._max_tokens.get(tier, 0)
        if cap:
            payload["max_completion_tokens"] = cap

        start = time.perf_counter()
        try:
            try:
                content, thoughts_streamed, usage, ttft_ms = self._stream(payload, model, start)
            except _StreamOptionsRefused:     # an endpoint that rejects stream_options: ask again without it
                payload.pop("stream_options")
                content, thoughts_streamed, usage, ttft_ms = self._stream(payload, model, start)
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach the LLM backend for {model}: {e}") from e
        latency_ms = int((time.perf_counter() - start) * 1000)

        text, reasoning_text = content, thoughts_streamed
        thoughts = THINK_RE.findall(text)
        if thoughts:
            reasoning_text = "\n".join([reasoning_text, *thoughts]).strip()
            text = THINK_RE.sub("", text).strip()

        estimated = not usage
        if estimated:  # backend didn't report usage; ~4 chars per token
            input_tokens = sum(len(m.get("content") or "") for m in messages) // 4
            output_tokens = (len(text) + len(reasoning_text)) // 4
        else:
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        reasoning_count = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))

        return LLMResult(
            text=text,
            tier=tier,
            model=model,
            latency_ms=latency_ms,
            reasoning=reasoning_text,
            usage=usage,
            ttft_ms=ttft_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_estimated=estimated,
            model_name=model,
            context_window=self._context_windows.get(tier) or None,
            est_cost_usd=self._cost(tier, input_tokens, output_tokens, cached),
            est_cost_if_ultra_usd=self._cost("ultra", input_tokens, output_tokens, cached),
            reasoning_on=effort != "minimal",
            reasoning_effort=effort,
            reasoning_token_count=reasoning_count,
            cached_tokens=cached,
        )

    def _stream(self, payload: dict, model: str, start: float) -> tuple[str, str, dict, Optional[int]]:
        """One streamed call: (answer text, streamed reasoning text, usage, time to first token ms)."""
        content_parts, reasoning_parts, usage = [], [], {}
        ttft_ms = None
        with self._http.stream("POST", "/chat/completions", json=payload) as response:
            if response.status_code >= 400:
                response.read()
                if response.status_code == 400 and "stream_options" in response.text and "stream_options" in payload:
                    raise _StreamOptionsRefused()
                raise LLMError(f"{model} returned {response.status_code}: {response.text[:200]}")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise LLMError(f"{model} stream error: {str(chunk['error'])[:200]}")
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if (piece or thought) and ttft_ms is None:
                        ttft_ms = int((time.perf_counter() - start) * 1000)
                    content_parts.append(piece)
                    reasoning_parts.append(thought)
        return "".join(content_parts), "".join(reasoning_parts), usage, ttft_ms

    def _cost(self, tier: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> Optional[float]:
        """Dollar cost of these tokens at the tier's per-1M-token prices (None if unpriced).
        Cached input tokens are billed at the cheaper cached rate."""
        price = self._prices.get(tier)
        if not price:
            return None
        cached = min(cached_tokens, input_tokens)
        cached_price = price.get("cached", price["in"])
        return ((input_tokens - cached) * price["in"] + cached * cached_price + output_tokens * price["out"]) / 1_000_000


class _StreamOptionsRefused(Exception):
    """The endpoint rejected `stream_options`; the call is repeated without it."""
