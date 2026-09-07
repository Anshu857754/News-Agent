"""The single place this application talks to OpenRouter.

Every stage of the future pipeline - startup relevance filtering, story
ranking, the editorial pass - calls one of the three methods here rather than
building its own client. That is the whole point of the module: one place where
the key is read, the timeout is set, errors are classified and calls are
logged. Scattering `AsyncOpenAI(...)` across modules is exactly the drift this
prevents.

Three entry points, in increasing order of structure:

* `generate_completion()`        -> free text
* `generate_structured_output()` -> a JSON object
* `analyze_content()`            -> structured analysis over a body of article
                                    text, with the truncation that bounds cost

OpenRouter is OpenAI wire-compatible, so the official SDK is the client; only
`base_url` and the key differ. There is no bespoke HTTP code here.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Optional

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# Models sometimes wrap JSON in prose or code fences; find the outermost object.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Characters of source text sent in one `analyze_content` call. Article bodies
# are unbounded; token spend should not be.
DEFAULT_MAX_INPUT_CHARS = 9000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class OpenRouterError(Exception):
    """Base class for every failure this service reports."""


class OpenRouterUnavailableError(OpenRouterError):
    """No key configured, or the call failed. Callers should degrade."""


class OpenRouterResponseError(OpenRouterError):
    """The model answered, but not with something usable."""


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply. Raises if there is not one.

    Handles the three failure shapes seen in practice: a bare object, an object
    inside ```json fences, and an object preceded by a sentence of commentary.
    """
    if not text or not text.strip():
        raise OpenRouterResponseError("model returned an empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(cleaned)
        if not match:
            raise OpenRouterResponseError(
                f"no JSON object in response: {text[:120]!r}"
            ) from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise OpenRouterResponseError(f"malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise OpenRouterResponseError(
            f"expected a JSON object, got {type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class OpenRouterService:
    """A reusable OpenRouter caller.

    Stateless between calls apart from the pooled HTTP client, so one instance
    is shared process-wide via `get_openrouter_service()`.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client: Any = None

    # -- configuration ------------------------------------------------------
    @property
    def model(self) -> str:
        return self.settings.openrouter_model.strip()

    @property
    def enabled(self) -> bool:
        """True when a call could actually succeed."""
        return self.settings.openrouter_enabled

    def describe(self) -> dict[str, object]:
        """Safe to log and to return from a health endpoint - never the key."""
        return {
            "provider": "openrouter",
            "model": self.model,
            "base_url": self.settings.openrouter_base_url,
            "key_present": bool(self.settings.openrouter_api_key.strip()),
            "timeout_seconds": self.settings.openrouter_timeout_seconds,
            "enabled": self.enabled,
        }

    # -- transport ----------------------------------------------------------
    def _client_or_raise(self) -> Any:
        """Built lazily, so importing this module never requires a key."""
        if self._client is None:
            if not self.enabled:
                raise OpenRouterUnavailableError(
                    "OpenRouter is not configured: set OPENROUTER_API_KEY "
                    "in .env (see .env.example)"
                )
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise OpenRouterUnavailableError(
                    "the 'openai' package is not installed; "
                    "run pip install -r backend/requirements.txt"
                ) from exc

            self._client = AsyncOpenAI(
                api_key=self.settings.openrouter_api_key,
                base_url=self.settings.openrouter_base_url,
                timeout=self.settings.openrouter_timeout_seconds,
                max_retries=self.settings.openrouter_max_retries,
                default_headers={"X-Title": self.settings.app_name},
            )
        return self._client

    async def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> str:
        """One chat completion. Returns the raw message content.

        Every SDK failure - timeout, rate limit, bad key, network - becomes
        `OpenRouterUnavailableError`, because the caller's job is to degrade,
        not to interpret an httpx exception.
        """
        client = self._client_or_raise()
        chosen = model or self.model

        kwargs: dict[str, Any] = {
            "model": chosen,
            "temperature": (
                self.settings.openrouter_temperature
                if temperature is None
                else temperature
            ),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            # Requested, never relied on: not every model on the router honours
            # it, so the reply still goes through the tolerant extractor.
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - every SDK error means "degrade"
            log.warning(
                "OpenRouter call failed (model=%s): %s: %s",
                chosen, type(exc).__name__, exc,
            )
            raise OpenRouterUnavailableError(f"{type(exc).__name__}: {exc}") from exc

        if not response.choices:
            raise OpenRouterResponseError("model returned no choices")

        usage = getattr(response, "usage", None)
        log.info(
            "OpenRouter ok (model=%s, tokens=%s)",
            chosen, getattr(usage, "total_tokens", "n/a"),
        )
        return response.choices[0].message.content or ""

    # -- public API ---------------------------------------------------------
    async def generate_completion(
        self,
        prompt: str,
        *,
        system_prompt: str = "You are a helpful assistant.",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Free-text completion."""
        text = await self._chat(
            system_prompt, prompt,
            model=model, temperature=temperature,
            max_tokens=max_tokens, json_mode=False,
        )
        if not text.strip():
            raise OpenRouterResponseError("model returned an empty completion")
        return text.strip()

    async def generate_structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """A JSON object from the model.

        The caller is expected to validate the dict against a Pydantic schema;
        this guarantees only that JSON came back.
        """
        raw = await self._chat(
            system_prompt, user_prompt,
            model=model, temperature=temperature,
            max_tokens=max_tokens, json_mode=True,
        )
        return extract_json(raw)

    async def analyze_content(
        self,
        content: str,
        instruction: str,
        *,
        model: Optional[str] = None,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Run `instruction` over `content` and return structured JSON.

        The workhorse for the future pipeline: startup-relevance scoring, story
        ranking and per-article analysis are all this call with a different
        instruction. Input is truncated so one long article cannot blow up the
        cost of a run.
        """
        if not content.strip():
            raise OpenRouterResponseError("nothing to analyse: content was empty")

        excerpt = content.strip()[:max_input_chars]
        if len(content) > max_input_chars:
            log.debug(
                "analyze_content truncated %d chars to %d",
                len(content), max_input_chars,
            )

        system_prompt = (
            "You are an analyst. Follow the instruction exactly and reply with "
            "a single JSON object and nothing else."
        )
        user_prompt = f"Instruction:\n{instruction}\n\nContent:\n{excerpt}"
        return await self.generate_structured_output(
            system_prompt, user_prompt, model=model, max_tokens=max_tokens
        )


@lru_cache(maxsize=1)
def get_openrouter_service() -> OpenRouterService:
    """The process-wide instance, so the HTTP client is built once."""
    return OpenRouterService()
