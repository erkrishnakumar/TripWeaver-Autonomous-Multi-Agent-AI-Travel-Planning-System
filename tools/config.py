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

    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://tripweaver:tripweaver@localhost:5432/tripweaver"
    )

    ollama_base_url: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "llama3.1")

    def validate_duffel(self) -> None:
        if not self.duffel_api_key:
            raise RuntimeError(
                "DUFFEL_API_KEY is not set. Copy .env.example to .env and add your "
                "sandbox token from https://app.duffel.com/join"
            )
        if not self.duffel_api_key.startswith("duffel_test_"):
            raise RuntimeError(
                "DUFFEL_API_KEY does not look like a sandbox token "
                "(expected it to start with 'duffel_test_'). Refusing to run against "
                "a possible live token from this script."
            )


settings = Settings()
