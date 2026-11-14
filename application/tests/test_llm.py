import pytest

from bot.llm import LLM, LLMError


def test_non_json_endpoint_reply_is_a_clean_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    llm = LLM()
    llm.client.chat.completions.create = lambda **kw: "OK"  # text/plain body aane par SDK yahi deta hai
    with pytest.raises(LLMError, match="non-JSON"):
        llm.json("s", "u")
