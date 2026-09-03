"""
Centralized environment/config loading. Every module reads settings from
here instead of calling os.environ directly, so there's one place to check
when something is misconfigured.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    duffel_api_key: str = os.environ.get("DUFFEL_API_KEY", "")
    duffel_base_url: str = "https://api.duffel.com"
    duffel_api_version: str = "v2"

    use_mock_data: bool = os.environ.get("USE_MOCK_DATA", "false").lower() in ("1", "true", "yes")

    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://tripweaver:tripweaver@localhost:5435/tripweaver"
    )

    # Which backend the CrewAI researcher/planner agents reason with.
    # Defaults to "ollama" (local-first, per the project's stated
    # architecture) — set LLM_PROVIDER=groq or LLM_PROVIDER=gemini in .env
    # to switch. This is independent of Groq's OTHER, narrow use in
    # check_visa_requirements() (app/tools/visa.py), which always uses
    # Groq directly regardless of this setting.
    llm_provider: str = os.environ.get("LLM_PROVIDER", "ollama").lower()

    # Optional override for the PLANNER specifically. Empty string (the
    # default) means "use the same provider as the researcher" (llm_provider
    # above). Set this to split load across two different providers/API keys
    # -- e.g. research on Gemini, planning on Groq -- since each Crew consumes
    # its own provider's quota independently.
    planner_llm_provider: str = os.environ.get("PLANNER_LLM_PROVIDER", "").lower()

    # Celery broker (task queue) + result backend, both pointing at the
    # same Valkey instance (docker-compose.yml's valkey service) -- see
    # app/worker/celery_app.py for how these get used. Default matches the
    # docker-compose port mapping (6380 on the host -> 6379 in the
    # container), not Valkey/Redis's usual default port, to avoid
    # colliding with any other Redis/Valkey instance already running
    # locally on this machine.
    celery_broker_url: str = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6380/0")
    celery_result_backend: str = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6380/1")

    # Rate limiting (Phase 9) -- a THIRD Valkey DB index (0/1 are already
    # Celery's broker/backend), same instance, so this needs no new
    # infrastructure. Defaults to matching celery_broker_url's host:port
    # with db=2, but is independently overridable in case rate-limit state
    # should ever live somewhere else entirely.
    rate_limit_storage_url: str = os.environ.get(
        "RATE_LIMIT_STORAGE_URL", "redis://localhost:6380/2"
    )

    # Optional per-task-category overrides for the RESEARCH crew specifically
    # (flight/car/hotel/context/formatter each run as their own Agent -- see
    # crew.py's build_research_crew()). Each is empty by default, meaning
    # "fall back to llm_provider". This exists to (a) spread load across
    # multiple providers' free-tier quotas instead of exhausting one, and
    # (b) let money-committing, hallucination-sensitive tasks (flight/car
    # search, and especially the formatter's exact-copy step) run on a
    # stronger hosted model while lower-stakes, informational tasks
    # (hotel search, weather/visa/ground-transport) run on a local model.
    # Use settings.resolve_research_provider(override) rather than reading
    # these directly, so the fallback-to-llm_provider behavior is applied
    # consistently everywhere.
    research_flight_llm_provider: str = os.environ.get("RESEARCH_FLIGHT_LLM_PROVIDER", "").lower()
    research_car_llm_provider: str = os.environ.get("RESEARCH_CAR_LLM_PROVIDER", "").lower()
    research_hotel_llm_provider: str = os.environ.get("RESEARCH_HOTEL_LLM_PROVIDER", "").lower()
    research_context_llm_provider: str = os.environ.get("RESEARCH_CONTEXT_LLM_PROVIDER", "").lower()
    research_formatter_llm_provider: str = os.environ.get(
        "RESEARCH_FORMATTER_LLM_PROVIDER", ""
    ).lower()

    def resolve_research_provider(self, override: str) -> str:
        """Returns `override` if set, else falls back to llm_provider --
        the single place every per-task-category override above is
        resolved, so the fallback rule can't drift between call sites."""
        return override or self.llm_provider

    ollama_base_url: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "llama3.1")

    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    # Only used when LLM_PROVIDER=groq (agent reasoning) -- distinct from
    # visa.py's own hardcoded, unrelated Groq model for its one-off lookup.
    groq_agent_model: str = os.environ.get("GROQ_AGENT_MODEL", "openai/gpt-oss-120b")

    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
    gemini_model: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # Used by estimate_ground_transport() (app/tools/ground_transport.py).
    # These are deliberately rough, global placeholder defaults — not tuned
    # to any specific city/country — since the tool itself only ever
    # returns a disclaimed estimate, never a real fare. Override via env if
    # you want figures that feel more realistic for a specific market.
    ground_transport_rate_per_km_usd: float = float(
        os.environ.get("GROUND_TRANSPORT_RATE_PER_KM_USD", "0.6")
    )
    ground_transport_min_fare_usd: float = float(
        os.environ.get("GROUND_TRANSPORT_MIN_FARE_USD", "3.0")
    )

    # Observability (Phase 6) -- see app/logging_config.py, the ONE place
    # this is read. Standard Python logging level names (DEBUG/INFO/
    # WARNING/ERROR); INFO by default shows every task/request lifecycle
    # event this project logs without CrewAI's own DEBUG-level tool-call
    # chatter drowning it out.
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    # JWT auth (Phase 8) -- see docs/Auth_Requirement.md for why this exists
    # at all: Duffel's own Service Agreement requires a closed, authenticated
    # user group, not just "nice to have" API hygiene. No default secret is
    # provided on purpose -- validate_jwt() below refuses to start without
    # one, rather than silently signing tokens with a guessable value.
    jwt_secret_key: str = os.environ.get("JWT_SECRET_KEY", "")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = int(
        os.environ.get("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    )
    # Deliberately much shorter than the access token above -- a reset
    # token grants "set a new password entirely," a more sensitive
    # capability than a normal session, so it should be usable for a much
    # narrower window even though (see docs/Auth_Requirement.md) it's
    # currently returned directly in the API response rather than emailed.
    password_reset_token_expire_minutes: int = int(
        os.environ.get("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", "30")
    )

    def validate_jwt(self) -> None:
        if not self.jwt_secret_key:
            raise RuntimeError(
                "JWT_SECRET_KEY is not set. Copy .env.example to .env and set it to a "
                'long random value (e.g. `python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"`).'
            )

    # Sends the real password-reset email (app/email/send_email.py). No
    # default sender either -- resend_from_email defaults to their shared
    # test sender (works immediately, no domain verification) but the API
    # key itself must be explicit, same "refuse to silently misbehave"
    # posture as jwt_secret_key/duffel_api_key above.
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
    resend_from_email: str = os.environ.get(
        "RESEND_FROM_EMAIL", "TripWeaver <onboarding@resend.dev>"
    )

    # Origins allowed to call the API from a browser (the frontend's dev
    # server and, in prod, its deployed origin) -- comma-separated, since env
    # vars can't carry a real list. Defaults cover `npm run dev` locally.
    cors_allowed_origins: list[str] = os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")

    def validate_resend(self) -> None:
        if not self.resend_api_key:
            raise RuntimeError(
                "RESEND_API_KEY is not set. Sign up free at https://resend.com, create an "
                "API key, and add it to .env."
            )

    def validate_duffel(self) -> None:
        if not self.duffel_api_key:
            raise RuntimeError(
                "DUFFEL_API_KEY is not set. Copy .env.example to .env and add your "
                "sandbox token from https://app.duffel.com/join (Developers > Access tokens)."
            )
        if not self.duffel_api_key.startswith("duffel_test_"):
            raise RuntimeError(
                "DUFFEL_API_KEY does not look like a sandbox token "
                "(expected it to start with 'duffel_test_')."
            )


settings = Settings()
