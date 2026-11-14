from fastapi.testclient import TestClient


def test_api_serves_page_and_policy_list():
    import app as appmod
    c = TestClient(appmod.app)
    assert "Weather Advisory Bot" in c.get("/").text
    j = c.get("/api/sops").json()
    assert len(j["sops"]) >= 10 and j["error"] is None
    assert c.post("/api/chat", json={"session_id": "", "message": "x"}).status_code == 422


def test_missing_api_key_gives_a_friendly_chat_reply(monkeypatch):
    import app as appmod
    for k in ("LLM_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    appmod.get_graph.cache_clear()
    r = TestClient(appmod.app).post("/api/chat", json={"session_id": "s", "message": "hi"})
    assert r.status_code == 200 and "Setup problem" in r.json()["reply"] and ".env" in r.json()["reply"]
    appmod.get_graph.cache_clear()
