from backend.llm_clients import get_model_choices


def test_provider_model_choices_are_available():
    assert "llama-3.3-70b-versatile" in get_model_choices("Groq")
    assert "gemini-2.5-flash" in get_model_choices("Gemini")
    assert get_model_choices("Local Expert System") == []
