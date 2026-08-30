"""
Package-level patch, applied before any agent code runs: works around a
known, still-open CrewAI bug (github.com/crewAIInc/crewAI/issues/5886)
where mark_cache_breakpoint() tags every outbound message with a
cache_breakpoint flag that only CrewAI's Anthropic adapter knows to strip
before sending. Every other provider routed through CrewAI's generic
litellm/OpenAI-compatible fallback path (Groq, in this project's case)
sends that flag through raw, and the real API rejects the request:
    'messages.0': property 'cache_breakpoint' is unsupported

Fix: make mark_cache_breakpoint() a no-op (return the message unchanged)
so the flag is never added for any provider. The only cost is disabling
CrewAI's optional Anthropic prompt-caching optimization -- a latency/cost
optimization, not a correctness feature, and this project doesn't use
Anthropic today.

crew_agent_executor.py imports mark_cache_breakpoint via a LOCAL
(function-body) `from crewai.llms.cache import mark_cache_breakpoint`,
re-executed on every call -- not a module-level import cached once. That
means patching the attribute on the crewai.llms.cache module itself, before
any agent ever executes, is picked up correctly on every subsequent call.

REMOVE THIS once CrewAI ships a real fix for #5886 (a strip step in the
generic/OpenAI-compatible adapter, mirroring the Anthropic adapter) --
check the issue's status before assuming this is still needed after any
future crewai upgrade.
"""

from typing import Any

import crewai.llms.cache as _crewai_cache


def _identity_mark_cache_breakpoint(message: dict[str, Any]) -> dict[str, Any]:
    return message


_crewai_cache.mark_cache_breakpoint = _identity_mark_cache_breakpoint
