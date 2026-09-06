from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from .config import ExperimentConfig


class APIError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    content: str
    response_model: str
    usage: dict[str, Any]
    request_id: str | None
    latency_seconds: float


class OpenAICompatibleClient:
    def __init__(self, config: ExperimentConfig):
        self.config = config
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        self.client = httpx.Client(
            base_url=config.api.base_url.rstrip("/"),
            headers=self.headers,
            timeout=config.api.timeout_seconds,
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_models(self) -> list[str]:
        response = self._request("GET", self.config.api.models_path)
        body = response.json()
        models = body.get("data", []) if isinstance(body, dict) else []
        return sorted(
            str(item["id"]) for item in models if isinstance(item, dict) and item.get("id")
        )

    def complete(
        self,
        prompt: str,
        *,
        temperature: float,
        top_p: float,
        max_tokens: int,
        model: str | None = None,
    ) -> Completion:
        payload = {
            "model": model or self.config.generator_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        started = time.monotonic()
        response = self._request("POST", self.config.api.chat_completions_path, json=payload)
        latency = time.monotonic() - started
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise APIError(f"Unexpected completion response shape: {exc}") from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        text = str(content).strip()
        if not text:
            raise APIError("Model returned an empty completion")
        return Completion(
            content=text,
            response_model=str(body.get("model") or payload["model"]),
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else {},
            request_id=response.headers.get("x-request-id"),
            latency_seconds=latency,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.config.api.max_retries + 1):
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code < 400:
                    return response
                location_pool_error = (
                    response.status_code == 400
                    and "User location is not supported" in response.text
                )
                if (
                    response.status_code
                    not in {408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
                    and not location_pool_error
                ):
                    raise APIError(
                        f"API returned HTTP {response.status_code}: {response.text[:500]}"
                    )
                last_error = APIError(
                    f"retryable HTTP {response.status_code}: {response.text[:200]}"
                )
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < self.config.api.max_retries:
                delay = min(30.0, (2**attempt) + random.random())
                time.sleep(delay)
        raise APIError(f"API request failed after retries: {last_error}")
