"""FastAPI server: /api/chat, /api/sops aur chat page yahin se serve hota hai. Chalane ke liye: uvicorn app:app --reload"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot.graph import build_graph, chat, default_deps
from bot.llm import LLMError

from functools import lru_cache

from bot.sops import SopStore

store = SopStore()


@lru_cache(maxsize=1)
def get_graph():  # lazy bana rahe hain taaki bina API key ke bhi app import ho jaye
    return build_graph(default_deps())  # MemorySaver: memory process ke andar rehti hai, session_id se alag alag


app = FastAPI(title="Weather-Advisory Support Bot")


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)


@app.post("/api/chat")
def api_chat(body: ChatIn):
    try:
        graph = get_graph()
    except LLMError as e:  # key ya .env nahi mila to 500 ki jagah chat me hi bata do
        return {"reply": f"Setup problem: {e}. Create application/.env (copy .env.example), set LLM_API_KEY, "
                         "then restart the server.\n\nPolicy basis: none (no advice given).",
                "trace": {"kind": "failure_config", "error": str(e)}}
    return chat(graph, body.session_id, body.message.strip())


@app.get("/api/sops")
def api_sops():
    store.refresh()
    return {"error": store.error,
            "sops": [{k: s.get(k) for k in ("id", "category", "title", "severity", "applies_to")} for s in store.sops]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
