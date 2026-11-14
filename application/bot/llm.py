"""LLM client (OpenAI-compatible, jaise Groq). API key .env se aati hai, code me nahi."""
from __future__ import annotations

import json
import os
import time

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

load_dotenv()


class LLMError(Exception):
    pass


class LLM:
    def __init__(self):
        key = os.getenv("LLM_API_KEY") or os.getenv("GITHUB_TOKEN")
        if not key:
            raise LLMError("GITHUB_TOKEN (or LLM_API_KEY) missing - copy .env.example to .env and fill it in")
        self.client = OpenAI(base_url=os.getenv("LLM_BASE_URL") or os.getenv("GITHUB_MODELS_API_BASE_URL", "https://models.github.ai/inference"),
                             api_key=key, max_retries=0, timeout=40)
        self.fast = os.getenv("AI_MODEL_FAST", "openai/gpt-4.1-mini")  
        self.smart = os.getenv("AI_MODEL_SMART", "openai/gpt-4.1")      

    def _chat(self, model: str, system: str, user: str, json_mode: bool) -> str:
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        for attempt in range(4):
            try:
                r = self.client.chat.completions.create(
                    model=model, temperature=0,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)
                if isinstance(r, str): 
                    raise LLMError(f"endpoint {self.client.base_url} returned non-JSON: {r[:200]!r}")
                return r.choices[0].message.content or ""
            except RateLimitError as e:  
                wait = min(float(e.response.headers.get("retry-after", 5)), 30)
                if attempt == 3:
                    raise LLMError(f"rate limited: {e}") from e
                time.sleep(wait)
            except (APIConnectionError, APIStatusError) as e:
                if attempt == 3 or (isinstance(e, APIStatusError) and e.status_code < 500):
                    raise LLMError(f"{type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)
        raise LLMError("unreachable")

    def json(self, system: str, user: str) -> dict:
        raw = self._chat(self.fast, system, user, json_mode=True)
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise LLMError(f"model returned invalid JSON: {raw[:120]}") from e
        if not isinstance(out, dict):
            raise LLMError("model returned non-object JSON")
        return out

    def text(self, system: str, user: str) -> str:
        return self._chat(self.smart, system, user, json_mode=False).strip()
