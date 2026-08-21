"""
check_visa_requirements() — the ONE tool in app/tools/ that calls an LLM.

Every other tool here (flights, weather, hotels) is deterministic: same
input always produces the same kind of API call and response shape. Visa
requirements are different — there is no free, live, authoritative visa API
available, and a hand-maintained static file would go stale immediately
with no way to detect it. Given that reality, this tool asks an LLM for a
best-effort, general-knowledge answer, and wraps the response in explicit,
unavoidable disclaimers so neither the agent layer nor the end traveler
mistakes it for verified data.

*** THIS TOOL'S OUTPUT IS NEVER AUTHORITATIVE. SEE VisaCheckResult IN
    schemas.py FOR THE FULL RATIONALE. ***

Uses Groq (cloud) rather than the project's local Ollama model — this is a
deliberate, narrow exception to the project's local-first principle. A
one-off factual lookup like this doesn't justify the latency/RAM cost of
running it through the same CPU-bound qwen2.5:3b that handles the main
conversational loop, and Groq's free tier + LPU inference make this call
fast (typically well under a second) without competing for the machine's
limited resources. Only passport_country + destination_country + purpose
(plain country names, no PII) leave the machine for this call.

Set USE_MOCK_DATA=true in .env to skip the real Groq call entirely and
return a canned response instead — useful for offline dev/demos, same
principle as flights.py and hotels.py's mock modes.
"""

from __future__ import annotations

import json
from pathlib import Path

from groq import APIStatusError, APITimeoutError, Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.tools.schemas import ToolError, TravelPurpose, VisaCheckInput, VisaCheckResult

_GROQ_MODEL = "llama-3.1-8b-instant"
_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "visa_responses.json"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_SYSTEM_PROMPT = """You are a travel visa information assistant. You will be given a \
passport country, a destination country, and a travel purpose. Respond with \
ONLY a JSON object, no other text, no markdown fences, in exactly this shape:

{"visa_required": true, "summary": "one or two plain sentences explaining the situation"}

Rules:
- "visa_required" must be true, false, or null.
- Use null if the answer is genuinely ambiguous, conditional on factors you \
don't have (e.g. depends on length of stay, or a program you're unsure is \
still active), or you are not confident.
- "summary" should be factual and neutral, 1-2 sentences, no hedging language \
like "I think" or "probably" inside the summary itself — if you're unsure, \
that's what the null value is for.
- Do not include any text outside the JSON object."""


def _is_retryable_groq_error(exc: BaseException) -> bool:
    """Only retry errors explicitly flagged retryable (429/5xx) — never
    retry on 4xx client errors like bad auth or malformed requests."""
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_groq_error),
    reraise=True,
)
def _call_groq(query: VisaCheckInput) -> str:
    client = Groq(api_key=settings.groq_api_key)
    user_prompt = (
        f"Passport country: {query.passport_country}\n"
        f"Destination country: {query.destination_country}\n"
        f"Travel purpose: {query.purpose.value}"
    )
    response = client.chat.completions.create(
        model=_GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_completion_tokens=200,
    )
    return response.choices[0].message.content or ""


def _parse_groq_response(raw_text: str, query: VisaCheckInput) -> VisaCheckResult:
    """Groq is instructed to return raw JSON, but LLMs occasionally wrap
    output in markdown fences despite instructions — strip those
    defensively before parsing rather than letting a cosmetic formatting
    slip crash the whole tool call."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    parsed = json.loads(cleaned)

    return VisaCheckResult(
        passport_country=query.passport_country,
        destination_country=query.destination_country,
        purpose=query.purpose,
        visa_required=parsed.get("visa_required"),
        summary=parsed.get("summary", "No summary provided."),
        model=_GROQ_MODEL,
    )


def _load_mock_response(query: VisaCheckInput) -> VisaCheckResult:
    with open(_FIXTURES_PATH) as f:
        fixtures = json.load(f)
    key = f"{query.passport_country.upper()}-{query.destination_country.upper()}"
    data = fixtures.get(key, fixtures["DEFAULT"])
    return VisaCheckResult(
        passport_country=query.passport_country,
        destination_country=query.destination_country,
        purpose=query.purpose,
        visa_required=data.get("visa_required"),
        summary=data.get("summary", "No summary provided."),
        model="mock",
    )


def check_visa_requirements(query: VisaCheckInput) -> VisaCheckResult | ToolError:
    """
    Get an informational, non-authoritative estimate of visa requirements
    for a passport/destination/purpose combination.

    Returns VisaCheckResult on success, or ToolError on failure so callers
    (agents, API handlers) can branch on outcome without a try/except around
    every call site — same contract as search_flights() and
    get_weather_forecast().

    ALWAYS treat the result as a starting point, never a final answer — see
    VisaCheckResult.disclaimer, which is present on every successful result
    and should be surfaced to the traveler, not silently dropped.

    Behavior depends on settings.use_mock_data:
      - True:  returns a local canned response, no network call
      - False: calls the real Groq API (requires GROQ_API_KEY)
    """
    if settings.use_mock_data:
        return _load_mock_response(query)

    if not settings.groq_api_key:
        return ToolError(
            tool_name="check_visa_requirements",
            error_type="config_error",
            message=(
                "GROQ_API_KEY is not set. Get a free key from "
                "https://console.groq.com/keys and add it to .env."
            ),
            retryable=False,
        )

    try:
        raw_text = _call_groq(query)
    except APITimeoutError:
        return ToolError(
            tool_name="check_visa_requirements",
            error_type="timeout",
            message="Groq API did not respond in time",
            retryable=True,
        )
    except APIStatusError as e:
        retryable = e.status_code in _RETRYABLE_STATUS_CODES
        return ToolError(
            tool_name="check_visa_requirements",
            error_type="groq_api_error",
            message=f"Groq API returned {e.status_code}: {str(e)[:300]}",
            retryable=retryable,
        )

    try:
        return _parse_groq_response(raw_text, query)
    except (json.JSONDecodeError, KeyError) as e:
        return ToolError(
            tool_name="check_visa_requirements",
            error_type="malformed_llm_response",
            message=f"Groq returned a response that couldn't be parsed as expected JSON: {e}",
            retryable=True,
        )


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.visa`
    # Add USE_MOCK_DATA=true to .env to test without hitting the real API.
    demo_query = VisaCheckInput(
        passport_country="India",
        destination_country="Thailand",
        purpose=TravelPurpose.TOURISM,
    )
    result = check_visa_requirements(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(f"{result.passport_country} -> {result.destination_country} ({result.purpose.value})")
        print(f"Visa required: {result.visa_required}")
        print(f"Summary: {result.summary}")
        print(f"\n{result.disclaimer}")
