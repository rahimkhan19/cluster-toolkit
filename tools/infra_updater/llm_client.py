# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Single Gemini client wrapper: model/project/location from config, retry with backoff
on transient errors, and schema-validated JSON output.

Uses GEMINI_API_KEY when set, otherwise Vertex AI with Application Default Credentials.
"""

import os
import re
import time
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from config import get_config

T = TypeVar("T", bound=BaseModel)

_RETRIABLE_MARKERS = ("429", "resource_exhausted", "rate_limit", "quota", "503", "unavailable", "preempted", "deadline")


def clean_error_message(ex: BaseException) -> str:
    """Concise, user-facing error message (no raw JSON or stack traces)."""
    raw = str(ex).strip()
    low = raw.lower()
    if "429" in raw or "resource_exhausted" in low or "quota" in low or "rate_limit" in low:
        return "Gemini API rate limit or quota exceeded (429 RESOURCE_EXHAUSTED). Please retry shortly."
    if "preempted" in low:
        return "Vertex AI inference preempted by cluster capacity (DECODE_PREEMPTED). Please retry."
    if "503" in raw or "unavailable" in low:
        return "Gemini service temporarily unavailable (503 UNAVAILABLE). Please retry shortly."
    if "401" in raw or "403" in raw or "permission_denied" in low:
        return "Authentication or permission error when contacting Gemini API (401/403)."
    if "deadline" in low or "504" in raw:
        return "Gemini request timed out (DEADLINE_EXCEEDED). Please retry."
    m = re.search(r'"message":\s*"([^"]+)"', raw)
    msg = m.group(1).split("\n")[0].strip() if m else raw.split("\n")[0].split("[type.googleapis.com")[0].split("{")[0].strip().rstrip(".:")
    return (msg[:92] + "...") if len(msg) > 95 else (msg or "LLM generation encountered an unexpected error.")


class LLMClient:
    def __init__(self, model: str, project: str, location: str, max_retries: int = 3, initial_delay: float = 2.0):
        from google import genai
        from google.genai import types
        self._types = types
        self.model = model
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        api_key = os.environ.get("GEMINI_API_KEY")
        self._client = (genai.Client(api_key=api_key) if api_key
                        else genai.Client(vertexai=True, project=project, location=location))

    def _generate(self, prompt: str, gen_config):
        delay = self.initial_delay
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._client.models.generate_content(model=self.model, contents=prompt, config=gen_config)
            except Exception as ex:  # SDK raises several error types; retry only transient ones.
                retriable = any(k in str(ex).lower() for k in _RETRIABLE_MARKERS)
                if not retriable or attempt == self.max_retries:
                    raise
                print(f"[WARN] Gemini transient error (attempt {attempt}/{self.max_retries}), retrying in {delay:.1f}s: {clean_error_message(ex)}", flush=True)
                time.sleep(delay)
                delay *= 2

    def generate_json(self, prompt: str, schema: Type[T], temperature: float = 0.0) -> T:
        """Structured output validated against the Pydantic schema."""
        resp = self._generate(prompt, self._types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema, temperature=temperature))
        return schema.model_validate_json(resp.text)

    def generate_text(self, prompt: str, temperature: float = 0.2, max_output_tokens: int = 1000) -> str:
        resp = self._generate(prompt, self._types.GenerateContentConfig(
            temperature=temperature, max_output_tokens=max_output_tokens))
        return (resp.text or "").strip()


def get_llm_client(model: Optional[str] = None) -> Optional[LLMClient]:
    """Returns an LLMClient from config, or None (with a warning) if the SDK or credentials are unavailable."""
    cfg = get_config().llm
    try:
        return LLMClient(model=model or cfg.model, project=cfg.project_id, location=cfg.location)
    except ImportError:
        print("[WARN] google-genai is not installed; LLM-based qualification is unavailable.", flush=True)
    except Exception as ex:  # Credential / client construction errors.
        print(f"[WARN] Failed to initialize Gemini client: {clean_error_message(ex)}", flush=True)
    return None
