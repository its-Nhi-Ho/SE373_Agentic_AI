import agent
from config import load_settings


def test_build_model_uses_openrouter_defaults(configured_env, monkeypatch):
    monkeypatch.setattr(agent, "ChatOpenAI", lambda **kwargs: kwargs)
    settings, missing = load_settings()

    assert missing == []
    assert settings is not None
    model = agent.build_model(settings)
    assert model == {
        "model": "mock-model",
        "api_key": "sk-test-not-a-real-key",
        "base_url": "https://openrouter.ai/api/v1",
    }


def test_build_model_allows_custom_openrouter_base_url(configured_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://api.example/v1")
    monkeypatch.setattr(agent, "ChatOpenAI", lambda **kwargs: kwargs)
    settings, missing = load_settings()

    assert missing == []
    assert settings is not None
    assert agent.build_model(settings)["base_url"] == "https://api.example/v1"


def test_load_settings_splits_and_strips_api_keys(lab_dirs, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", " first-key, second-key , , third-key ")
    monkeypatch.setenv("MODEL_NAME", "mock-model")

    settings, missing = load_settings()

    assert missing == []
    assert settings is not None
    assert settings.api_keys == ("first-key", "second-key", "third-key")
    assert settings.api_key == "first-key"
