from app.config import load_dotenv


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ANTHROPIC_API_KEY=\n"
        "CLAUDE_MODEL=claude-sonnet-5-5        # при перерасходе\n"
        'STUDIO_NAME="Gubska Tattoo # Studio"\n'
        "export PLAYGROUND_TOKEN=abc\n"
        "APPROVAL_MODE=false\n",
        encoding="utf-8",
    )
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "STUDIO_NAME", "PLAYGROUND_TOKEN", "APPROVAL_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APPROVAL_MODE", "true")  # окружение важнее файла
    load_dotenv(env)
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ  # пустое значение не задаётся
    assert os.environ["CLAUDE_MODEL"] == "claude-sonnet-5-5"
    assert os.environ["STUDIO_NAME"] == "Gubska Tattoo # Studio"
    assert os.environ["PLAYGROUND_TOKEN"] == "abc"
    assert os.environ["APPROVAL_MODE"] == "true"
