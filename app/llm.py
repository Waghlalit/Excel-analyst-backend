"""Model access, behind one interface.

Two backends — local Ollama and the hosted Mistral API — selected by
LLM_PROVIDER. Everything else in the codebase calls the four functions at the
bottom of this file and never learns which one is in use.

Adds two things a hosted API needs and a local one doesn't:
  * retry with exponential backoff on rate limits and transient 5xx
  * batching, so a 400-column workbook doesn't become one enormous request
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Iterator
from typing import Any, Literal

from . import config

log = logging.getLogger(__name__)

Role = Literal["classifier", "sql", "narrative"]

_MODEL_FOR: dict[str, str] = {
    "classifier": config.CLASSIFIER_MODEL,
    "sql": config.SQL_MODEL,
    "narrative": config.NARRATIVE_MODEL,
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when a provider call fails after exhausting retries."""


# --------------------------------------------------------------------------- retry


def _is_transient(error: Exception) -> bool:
    """Rate limits, timeouts and 5xx are worth retrying; 400s are not."""
    text = f"{type(error).__name__} {error}".lower()
    if any(token in text for token in ("429", "rate limit", "too many requests")):
        return True
    if any(token in text for token in ("timeout", "timed out", "connection", "temporarily")):
        return True
    return any(code in text for code in ("500", "502", "503", "504"))


def _with_retry(operation, description: str):
    """Run `operation`, retrying transient failures with jittered backoff."""
    last: Exception | None = None
    for attempt in range(config.API_MAX_RETRIES):
        try:
            return operation()
        except Exception as error:  # provider SDKs raise their own types
            last = error
            if not _is_transient(error) or attempt == config.API_MAX_RETRIES - 1:
                break
            # Exponential backoff with jitter, so parallel callers don't
            # retry in lockstep and re-trigger the same rate limit.
            delay = config.API_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.4)
            log.warning("%s failed (%s); retrying in %.1fs", description, error, delay)
            time.sleep(delay)

    raise LLMError(f"{description} failed after {config.API_MAX_RETRIES} attempts: {last}") from last


# --------------------------------------------------------------------------- backends
#
# Clients are created lazily so that running with LLM_PROVIDER=ollama does not
# require the mistralai package to be installed, and vice versa.

_ollama_client = None
_mistral_client = None


def _ollama():
    global _ollama_client
    if _ollama_client is None:
        import ollama

        _ollama_client = ollama.Client(host=config.OLLAMA_HOST)
    return _ollama_client


def _mistral():
    global _mistral_client
    if _mistral_client is None:
        if not config.MISTRAL_API_KEY:
            raise LLMError("LLM_PROVIDER=mistral but MISTRAL_API_KEY is not set.")

        # The SDK moved the client in v2: mistralai.client.Mistral. Try that
        # first, then fall back to the v1 top-level import.
        try:
            from mistralai.client import Mistral
        except ImportError:
            try:
                from mistralai import Mistral  # type: ignore[no-redef]
            except ImportError as error:  # pragma: no cover
                raise LLMError(
                    "The mistralai package is not installed. pip install mistralai"
                ) from error

        _mistral_client = Mistral(api_key=config.MISTRAL_API_KEY)
    return _mistral_client


# --------------------------------------------------------------------------- embeddings


def _embed_batch(texts: list[str]) -> list[list[float]]:
    if config.LLM_PROVIDER == "ollama":
        def call():
            return list(_ollama().embed(model=config.EMBED_MODEL, input=texts)["embeddings"])
    else:
        def call():
            response = _mistral().embeddings.create(model=config.EMBED_MODEL, inputs=texts)
            return [item.embedding for item in response.data]

    return _with_retry(call, f"embed({len(texts)} texts)")


def embed(texts: list[str]) -> list[list[float]]:
    """Embed any number of texts, chunked to stay inside per-request limits."""
    if not texts:
        return []

    vectors: list[list[float]] = []
    size = max(1, config.EMBED_BATCH_SIZE)
    for start in range(0, len(texts), size):
        chunk = texts[start : start + size]
        vectors.extend(_embed_batch(chunk))
        if start + size < len(texts):
            # Gentle pacing; free tiers meter per minute, not per request.
            time.sleep(0.05)

    if len(vectors) != len(texts):
        raise LLMError(f"Embedding count mismatch: sent {len(texts)}, received {len(vectors)}")
    return vectors


# --------------------------------------------------------------------------- chat


def complete(system: str, user: str, role: Role = "narrative", temperature: float = 0.1) -> str:
    model = _MODEL_FOR[role]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    if config.LLM_PROVIDER == "ollama":
        def call():
            response = _ollama().chat(
                model=model, messages=messages, options={"temperature": temperature}
            )
            return response["message"]["content"]
    else:
        def call():
            response = _mistral().chat.complete(
                model=model, messages=messages, temperature=temperature
            )
            return response.choices[0].message.content or ""

    return _with_retry(call, f"complete[{role}:{model}]").strip()


def stream(
    system: str, user: str, role: Role = "narrative", temperature: float = 0.3
) -> Iterator[str]:
    """Yield text chunks. Not retried — a partially streamed turn cannot be
    replayed cleanly, and the caller already surfaces errors to the user."""
    model = _MODEL_FOR[role]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    if config.LLM_PROVIDER == "ollama":
        for chunk in _ollama().chat(
            model=model, messages=messages, options={"temperature": temperature}, stream=True
        ):
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
    else:
        for event in _mistral().chat.stream(
            model=model, messages=messages, temperature=temperature
        ):
            piece = event.data.choices[0].delta.content
            if piece:
                # Some SDK versions deliver content as a list of chunks.
                yield piece if isinstance(piece, str) else "".join(map(str, piece))


def complete_json(system: str, user: str, role: Role = "classifier") -> dict[str, Any]:
    """Ask for JSON and parse it defensively.

    Smaller models wrap JSON in prose or code fences more often than not, so we
    fall back to extracting the outermost object rather than trusting the whole
    response.
    """
    raw = complete(
        system + "\n\nRespond with JSON only. No prose, no code fences.",
        user,
        role=role,
        temperature=0.0,
    )
    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    log.warning("Could not parse JSON from model output: %s", cleaned[:200])
    return {}


def strip_sql_fence(raw: str) -> str:
    """Pull SQL out of whatever wrapper the model put around it."""
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if blocks:
            text = blocks[0]
    return text.strip().rstrip(";").strip()


def health() -> dict[str, Any]:
    """Cheap reachability probe, surfaced on /api/health."""
    try:
        vector = embed(["health check"])
        return {"reachable": True, "embed_dimensions": len(vector[0]) if vector else 0}
    except Exception as error:
        return {"reachable": False, "error": f"{type(error).__name__}: {error}"}
