"""
Unit tests for app/tools/visa.py.

This is the one tool in app/tools/ that calls an LLM rather than a
deterministic API — the Groq client itself is mocked here (never a real
network call), same principle as httpx being mocked in the other test
files. This means CI can run these with zero secrets, zero cost, and zero
network flakiness, and there is no live-Groq-call test in this file for the
same reason there's no live-httpx-call test elsewhere: mocking the client
boundary is the correct level to test at.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.schemas import ToolError, TravelPurpose, VisaCheckInput, VisaCheckResult
from app.tools.visa import _parse_groq_response, check_visa_requirements


def _mock_groq_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


@pytest.fixture
def query() -> VisaCheckInput:
    return VisaCheckInput(
        passport_country="India", destination_country="Thailand", purpose=TravelPurpose.TOURISM
    )


@pytest.fixture(autouse=True)
def _force_real_api_mode():
    """Most tests exercise the Groq code path, so force mock mode off by
    default. The dedicated mock-mode tests below turn it back on
    explicitly. Mirrors the same fixture pattern in test_flights.py."""
    from app import config

    config.settings.use_mock_data = False
    config.settings.groq_api_key = "fake_key_for_tests"
    yield
    config.settings.use_mock_data = False


def test_returns_result_from_clean_json_response(query):
    with patch("app.tools.visa.Groq") as MockGroq:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_groq_response(
            '{"visa_required": false, "summary": "No visa needed for short tourist stays."}'
        )
        MockGroq.return_value = mock_client

        result = check_visa_requirements(query)

    assert isinstance(result, VisaCheckResult)
    assert result.visa_required is False
    assert result.summary == "No visa needed for short tourist stays."
    assert result.confidence_level == "informational_only"
    assert result.provider == "groq"
    assert result.model == "llama-3.1-8b-instant"


def test_disclaimer_is_always_present_and_non_empty(query):
    with patch("app.tools.visa.Groq") as MockGroq:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_groq_response(
            '{"visa_required": true, "summary": "Visa required."}'
        )
        MockGroq.return_value = mock_client

        result = check_visa_requirements(query)

    assert isinstance(result, VisaCheckResult)
    assert len(result.disclaimer) > 20
    assert "not" in result.disclaimer.lower()
    assert "official" in result.disclaimer.lower()


def test_null_visa_required_is_preserved_for_ambiguous_cases(query):
    """A null answer is a legitimate, honest outcome — the model saying
    'I don't know' should pass through as None, not get coerced to False
    or trigger an error."""
    with patch("app.tools.visa.Groq") as MockGroq:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_groq_response(
            '{"visa_required": null, "summary": "Depends on length of stay and current agreements."}'
        )
        MockGroq.return_value = mock_client

        result = check_visa_requirements(query)

    assert isinstance(result, VisaCheckResult)
    assert result.visa_required is None
    assert "depends" in result.summary.lower()


def test_strips_markdown_fences_from_response(query):
    """LLMs sometimes wrap JSON in markdown fences despite instructions not
    to — this should be handled gracefully, not crash the tool."""
    fenced_content = '```json\n{"visa_required": false, "summary": "Fenced response."}\n```'
    result = _parse_groq_response(fenced_content, query)

    assert result.visa_required is False
    assert result.summary == "Fenced response."


def test_malformed_response_returns_tool_error_not_exception(query):
    with patch("app.tools.visa.Groq") as MockGroq:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_groq_response(
            "this is not valid json at all"
        )
        MockGroq.return_value = mock_client

        result = check_visa_requirements(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "check_visa_requirements"
    assert result.error_type == "malformed_llm_response"
    assert result.retryable is True


def test_missing_api_key_returns_tool_error_not_exception(query):
    from app import config

    config.settings.groq_api_key = ""

    result = check_visa_requirements(query)

    assert isinstance(result, ToolError)
    assert result.tool_name == "check_visa_requirements"
    assert result.error_type == "config_error"
    assert result.retryable is False
    assert "GROQ_API_KEY" in result.message


def test_mock_mode_returns_fixture_data_with_no_network_call(query):
    """No Groq mock patched here on purpose — if the code accidentally
    tried a real network call in mock mode, this would fail since Groq()
    isn't even patched to accept a fake key gracefully at the network
    layer."""
    from app import config

    config.settings.use_mock_data = True

    result = check_visa_requirements(query)

    assert isinstance(result, VisaCheckResult)
    assert result.model == "mock"
    assert result.visa_required is False  # matches the INDIA-THAILAND fixture


def test_mock_mode_falls_back_to_default_for_unknown_route():
    from app import config

    config.settings.use_mock_data = True

    query = VisaCheckInput(
        passport_country="Wakanda", destination_country="Narnia", purpose=TravelPurpose.TOURISM
    )
    result = check_visa_requirements(query)

    assert isinstance(result, VisaCheckResult)
    assert result.visa_required is None
    assert "mock response" in result.summary.lower()


def test_purpose_defaults_to_tourism():
    query = VisaCheckInput(passport_country="India", destination_country="Thailand")
    assert query.purpose == TravelPurpose.TOURISM


def test_rejects_too_short_country_names():
    with pytest.raises(ValueError):
        VisaCheckInput(passport_country="I", destination_country="Thailand")