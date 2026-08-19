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

    ollama_base_url: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "llama3.1")

    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")

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