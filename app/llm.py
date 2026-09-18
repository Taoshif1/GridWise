from __future__ import annotations

import asyncio
import itertools
import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .config import Settings
from .schemas import BatteryInput, DirectiveInterpretation, LLMDirectiveEnvelope


class LLMServiceError(RuntimeError):
    pass


_KEY_START_COUNTER = itertools.count()


SYSTEM_PROMPT = r"""
You are the operator-note interpretation component of the GridWise smart-campus energy scheduler.

Your ONLY task is to convert each operator note into exactly one machine-checkable directive.
Do not optimize energy. Do not change base demand, base solar, tariff, or battery parameters.
Do not invent directive types.

ALLOWED DIRECTIVE TYPES AND EXACT structured_adjustment SHAPES:

1. solar_reduction
   {"hours":[...], "factor": number}
   `factor` is the fraction of usable solar REMAINING.
   "80% reduction" => factor 0.2.
   "drop to 20%" => factor 0.2.

2. minimum_battery_reserve
   {"hours":[...], "minimum_energy_kwh": number}

3. no_charge_window
   {"hours":[...]}

4. no_discharge_window
   {"hours":[...]}

5. max_grid_window
   {"hours":[...], "max_grid_kwh": number}

6. no_op
   structured_adjustment must be null.

SEMANTICS:
- Produce exactly one output entry for every input note.
- Preserve note_index order 0, 1, ..., N-1.
- Relevant supported rule => applies=true.
- Irrelevant note => applies=false, directive_type="no_op", structured_adjustment=null.
- Whole-hour windows are start-inclusive and end-exclusive.
  "1 PM to 3 PM" => [13,14].
  "2 PM until 4 PM" => [14,15].
  "13:00 to 15:00" => [13,14].
- Every hours array must use unique integers 0..23 in ascending order.
- Interpret paraphrases by meaning, not keyword matching.
- Do not force unsupported instructions into a supported type.
- Keep explanation short and factual.
""".strip()


DIRECTIVE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "directives": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {"type": "integer", "minimum": 0, "maximum": 2},
                    "applies": {"type": "boolean"},
                    "directive_type": {
                        "type": "string",
                        "enum": [
                            "solar_reduction",
                            "minimum_battery_reserve",
                            "no_charge_window",
                            "no_discharge_window",
                            "max_grid_window",
                            "no_op",
                        ],
                    },
                    "structured_adjustment": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "properties": {
                                    "hours": {
                                        "type": "array",
                                        "items": {"type": "integer", "minimum": 0, "maximum": 23},
                                    },
                                    "factor": {"type": "number", "minimum": 0, "maximum": 1},
                                    "minimum_energy_kwh": {"type": "number", "minimum": 0},
                                    "max_grid_kwh": {"type": "number", "minimum": 0},
                                },
                                "additionalProperties": False,
                            },
                        ]
                    },
                    "explanation": {"type": "string"},
                },
                "required": [
                    "note_index",
                    "applies",
                    "directive_type",
                    "structured_adjustment",
                    "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["directives"],
    "additionalProperties": False,
}


def _extract_json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMServiceError("Gemini did not return a JSON object")
    return text[start : end + 1]


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    try:
        candidates = payload["candidates"]
        parts = candidates[0]["content"]["parts"]
    except Exception as exc:
        raise LLMServiceError("Gemini response did not contain a usable candidate") from exc

    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])

    if not texts:
        raise LLMServiceError("Gemini response did not contain text output")
    return "\n".join(texts)


class GeminiInterpreter:
    """Direct Google Gemini GenerateContent integration with multi-key fallback."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _ordered_keys(self) -> list[str]:
        keys = self.settings.gemini_api_keys
        if not keys:
            return []

        # Start at a different key across requests instead of always hammering key #1.
        start = next(_KEY_START_COUNTER) % len(keys)
        ordered = keys[start:] + keys[:start]

        max_attempts = max(1, self.settings.gemini_max_key_attempts)
        return ordered[: min(len(ordered), max_attempts)]

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        model = quote(self.settings.gemini_model, safe="-_.")
        url = f"{self.settings.gemini_base_url}/models/{model}:generateContent"

        keys = self._ordered_keys()
        if not keys:
            raise LLMServiceError("No Gemini API key is configured")

        # We do not log, return, or otherwise expose the actual key values.
        retryable_key_statuses = {401, 403, 429}
        retryable_provider_statuses = {500, 502, 503, 504}

        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            for key_index, api_key in enumerate(keys):
                headers = {
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                }

                per_key_attempts = max(1, self.settings.llm_max_retries + 1)

                for retry in range(per_key_attempts):
                    try:
                        response = await client.post(url, headers=headers, json=body)

                        if response.status_code < 400:
                            return response.json()

                        # Invalid/expired/quota-limited key: immediately try the next configured key.
                        if response.status_code in retryable_key_statuses:
                            last_error = LLMServiceError(
                                f"Gemini key #{key_index + 1} returned HTTP {response.status_code}"
                            )
                            break

                        # Provider-side transient error: retry briefly, then move to another key.
                        if response.status_code in retryable_provider_statuses:
                            last_error = LLMServiceError(
                                f"Gemini provider returned HTTP {response.status_code}"
                            )
                            if retry + 1 < per_key_attempts:
                                await asyncio.sleep(0.25 * (retry + 1))
                                continue
                            break

                        # 400-class request/model/schema failures generally are not fixed by changing keys.
                        raise LLMServiceError(
                            f"Gemini API returned HTTP {response.status_code}"
                        )

                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        last_error = exc
                        if retry + 1 < per_key_attempts:
                            await asyncio.sleep(0.25 * (retry + 1))
                            continue
                        break

                    except (ValueError, LLMServiceError) as exc:
                        last_error = exc
                        if isinstance(exc, LLMServiceError):
                            raise
                        break

        if isinstance(last_error, LLMServiceError):
            raise LLMServiceError("All configured Gemini API keys/provider attempts failed")
        raise LLMServiceError("Gemini API request failed") from last_error

    async def interpret(
        self,
        notes: list[str],
        battery: BatteryInput,
    ) -> list[DirectiveInterpretation]:
        if not self.settings.gemini_api_keys:
            raise LLMServiceError("No Gemini API key is configured")

        user_payload = {
            "battery_capacity_kwh": battery.capacity_kwh,
            "operator_notes": [
                {"note_index": index, "text": note}
                for index, note in enumerate(notes)
            ],
        }

        body = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_PROMPT}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Interpret these operator notes and return only the required structured JSON.\n"
                                + json.dumps(user_payload, ensure_ascii=False)
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": DIRECTIVE_JSON_SCHEMA,
            },
        }

        payload = await self._post(body)
        content = _extract_gemini_text(payload)

        try:
            raw = json.loads(_extract_json_text(content))
            envelope = LLMDirectiveEnvelope.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, LLMServiceError) as exc:
            raise LLMServiceError(
                "Gemini returned malformed or unsupported structured output"
            ) from exc

        return envelope.directives


class DemoInterpreter:
    """
    Development-only deterministic interpreter.
    Do NOT submit with LLM_MODE=demo; the challenge requires a real LLM.
    """

    @staticmethod
    def _hour(hour: int, suffix: str) -> int:
        suffix = suffix.lower()
        if suffix == "am":
            return 0 if hour == 12 else hour
        return 12 if hour == 12 else hour + 12

    @classmethod
    def _window(cls, note: str):
        patterns = [
            r"(?:from|between)\s+(\d{1,2})\s*(am|pm)\s+(?:to|and|until)\s+(\d{1,2})\s*(am|pm)",
            r"(?:from|between)\s+(\d{1,2}):00\s+(?:to|and|until)\s+(\d{1,2}):00",
        ]
        m = re.search(patterns[0], note, re.I)
        if m:
            start = cls._hour(int(m.group(1)), m.group(2))
            end = cls._hour(int(m.group(3)), m.group(4))
            if end <= start:
                end += 24
            return sorted({h % 24 for h in range(start, end)})

        m = re.search(patterns[1], note, re.I)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if end <= start:
                end += 24
            return sorted({h % 24 for h in range(start, end)})
        return None

    async def interpret(self, notes: list[str], battery: BatteryInput):
        output: list[DirectiveInterpretation] = []

        for i, note in enumerate(notes):
            low = note.lower()
            hours = self._window(note)

            if hours and any(x in low for x in ["do not charge", "no charging", "charging is unavailable"]):
                output.append(DirectiveInterpretation(
                    note_index=i,
                    applies=True,
                    directive_type="no_charge_window",
                    structured_adjustment={"hours": hours},
                    explanation="Development parser detected a no-charge window.",
                ))
                continue

            if hours and any(x in low for x in ["do not discharge", "no discharging", "discharging is unavailable"]):
                output.append(DirectiveInterpretation(
                    note_index=i,
                    applies=True,
                    directive_type="no_discharge_window",
                    structured_adjustment={"hours": hours},
                    explanation="Development parser detected a no-discharge window.",
                ))
                continue

            reserve = re.search(r"(?:at least|reserve).*?(\d+(?:\.\d+)?)\s*kwh", low)
            if hours and reserve:
                output.append(DirectiveInterpretation(
                    note_index=i,
                    applies=True,
                    directive_type="minimum_battery_reserve",
                    structured_adjustment={
                        "hours": hours,
                        "minimum_energy_kwh": float(reserve.group(1)),
                    },
                    explanation="Development parser detected a minimum battery reserve.",
                ))
                continue

            grid_cap = re.search(r"(?:grid|import).*?(?:not exceed|maximum|max)\D*(\d+(?:\.\d+)?)\s*kwh", low)
            if hours and grid_cap:
                output.append(DirectiveInterpretation(
                    note_index=i,
                    applies=True,
                    directive_type="max_grid_window",
                    structured_adjustment={
                        "hours": hours,
                        "max_grid_kwh": float(grid_cap.group(1)),
                    },
                    explanation="Development parser detected a grid-import cap.",
                ))
                continue

            if hours and "solar" in low:
                to_percent = re.search(r"(?:drop|fall|reduc\w*)\s+to\s+(?:about\s+)?(\d+(?:\.\d+)?)\s*%", low)
                by_percent = re.search(r"(?:reduc\w*).*?by\s+(\d+(?:\.\d+)?)\s*%", low)
                factor = None
                if to_percent:
                    factor = float(to_percent.group(1)) / 100
                elif by_percent:
                    factor = 1 - float(by_percent.group(1)) / 100

                if factor is not None:
                    output.append(DirectiveInterpretation(
                        note_index=i,
                        applies=True,
                        directive_type="solar_reduction",
                        structured_adjustment={"hours": hours, "factor": factor},
                        explanation="Development parser detected a solar reduction.",
                    ))
                    continue

            output.append(DirectiveInterpretation(
                note_index=i,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation="Development parser found no supported current-schedule directive.",
            ))

        return output


def build_interpreter(settings: Settings):
    if settings.llm_mode == "gemini":
        return GeminiInterpreter(settings)
    if settings.llm_mode == "demo":
        return DemoInterpreter()
    raise LLMServiceError("LLM_MODE must be either 'gemini' or 'demo'")
