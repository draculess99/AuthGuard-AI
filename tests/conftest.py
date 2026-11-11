import pytest
import requests


@pytest.fixture(autouse=True)
def isolate_llm_and_network(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent of the developer's private .env and block real HTTP calls.

    Tests that need a provider call patch call_groq/requests.post explicitly.
    """
    for name in ("AUTHGUARD_ENABLE_GROQ", "GROQ_API_KEY", "GROQ_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("Real network request attempted during tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
