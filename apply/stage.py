"""Stagehand: an AI that fills one box by looking at the live page.

Our own filler (apply/cloud.py) knows each widget by code: how Keka's date box
wants a day, that "14 LPA" is 1400000 in a rupees box. Stagehand knows none of
that, but it reads the page and decides the click itself, so a box built a way
we never saw can still be filled. Used as the safety net under our filler
(`APPLY_FILLER=fallback`), or first (`stagehand`); `ours` never calls it.

What stays ours either way: the VALUE (shaped in code before Stagehand sees it),
the check that the box now holds it, the approval, and the submit guard.
Stagehand fills one box; it never submits and is never told to.

How it attaches: Stagehand needs its extension inside the browser, so the
session is created with it (`extension_id()`), and Stagehand then joins that
same session next to Playwright. The model is GLM through our own client (an
`llm.generate` callback), so no second AI account. Spiked 2026-10-04: GLM 5.3
filled two Greenhouse boxes, about 6.7k input tokens and 3-5 s each.

It is async and the browser is sync, so it runs on its own thread and loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx

import llm
import paths

API = "https://api.browserbase.com/v1"
#: "ours" (never Stagehand) | "fallback" (ours first, Stagehand when a box
#: fails) | "stagehand" (Stagehand first, ours when it fails).
FILLER_ENV = "APPLY_FILLER"
#: Which GLM model reads the page. Flash is ~9x cheaper; "" = the cheap slot.
MODEL_ENV = "STAGEHAND_MODEL"
ID_FILE = paths.ROOT / ".stagehand_extension_id"
ACT_SECONDS = 60


def filler() -> str:
    mode = os.environ.get(FILLER_ENV, "ours").strip().lower()
    return mode if mode in ("ours", "fallback", "stagehand") else "ours"


def available() -> bool:
    if filler() == "ours":
        return False
    try:
        import stagehand  # noqa: F401, PLC0415
    except ImportError:
        return False
    return bool(os.environ.get("BROWSERBASE_API_KEY"))


def extension_id() -> str:
    """Stagehand's browser extension, uploaded to our Browserbase project once
    and reused (it is the same file every time)."""
    headers = {"X-BB-API-Key": os.environ["BROWSERBASE_API_KEY"]}
    if ID_FILE.exists():
        known = ID_FILE.read_text().strip()
        if known and httpx.get(f"{API}/extensions/{known}", headers=headers,
                               timeout=20).status_code == 200:
            return known
    from stagehand.extension_assets import build_extension_archive  # noqa: PLC0415
    r = httpx.post(f"{API}/extensions", headers=headers, timeout=60,
                   files={"file": ("stagehand-extension.zip", build_extension_archive())})
    r.raise_for_status()
    ID_FILE.write_text(r.json()["id"])
    return r.json()["id"]


def _model() -> str:
    return os.environ.get(MODEL_ENV) or llm.models()[1]


def _text(block) -> str:
    return getattr(block.root if hasattr(block, "root") else block, "text", "") or ""


def _messages(params) -> list[dict]:
    """Stagehand's messages as the Anthropic-shaped API takes them. Images are
    left out: GLM's Anthropic endpoint drops them, and the page is read from
    its text anyway."""
    out = []
    for m in params.messages:
        blocks = m.content if isinstance(m.content, list) else [m.content]
        parts = []
        for b in blocks:
            b = b.root if hasattr(b, "root") else b
            kind = getattr(b, "type", None)
            if kind == "text":
                parts.append({"type": "text", "text": b.text})
            elif kind == "tool_use":
                parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif kind == "tool_result":
                parts.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                              "content": "".join(_text(x) for x in b.content) or "ok"})
        if parts:
            out.append({"role": getattr(m.role, "value", str(m.role)), "content": parts})
    return out


async def _generate(params):
    """Stagehand's `llm.generate` request, answered by our GLM client."""
    from stagehand._generated.models import (  # noqa: PLC0415
        LLMMessageGenerateResult, LLMStructuredGenerateResult, LLMTextContent,
        LLMToolUseContent, LLMUsage)
    import usage
    p = params.root if hasattr(params, "root") else params
    kw = dict(model=_model(), max_tokens=4096, messages=_messages(p),
              system=p.system_prompt or "")
    fmt = getattr(p, "response_format", None)
    structured = fmt is not None and fmt.type == "json_schema"
    if structured:
        schema = fmt.schema_
        schema = schema.model_dump(mode="json") if hasattr(schema, "model_dump") else schema
        kw["system"] += ("\n\nReply with ONLY a JSON object matching this JSON schema, "
                         "no prose, no code fence:\n" + json.dumps(schema))
    elif getattr(p, "tools", None):
        kw["tools"] = [{"name": t.name, "description": t.description or "",
                        "input_schema": t.input_schema.model_dump(mode="json")
                        if hasattr(t.input_schema, "model_dump") else t.input_schema}
                       for t in p.tools]
    # Low effort: no thinking block; picking a box is not a reasoning task.
    kw["output_config"] = {"effort": llm.effort_level("low")}
    llm.counted()
    r = await asyncio.to_thread(lambda: llm.client().messages.create(**kw))
    usage.record(r, model=kw["model"], purpose="stagehand")
    u = LLMUsage(input_tokens=r.usage.input_tokens, output_tokens=r.usage.output_tokens,
                 total_tokens=r.usage.input_tokens + r.usage.output_tokens)
    text = "".join(b.text for b in r.content if b.type == "text")
    if structured:
        body = text[text.find("{"): text.rfind("}") + 1]
        return LLMStructuredGenerateResult(
            role="assistant", content=[LLMTextContent(type="text", text=text)],
            stop_reason=r.stop_reason, usage=u, output_format="json_schema",
            structured_content=json.loads(body))
    blocks = [LLMTextContent(type="text", text=text)] if text else []
    blocks += [LLMToolUseContent(type="tool_use", id=b.id, name=b.name, input=b.input)
               for b in r.content if b.type == "tool_use"]
    return LLMMessageGenerateResult(role="assistant", content=blocks, stop_reason=r.stop_reason,
                                    usage=u, output_format="text")


class Stage:
    """One Stagehand attached to one Browserbase session, on its own thread."""

    def __init__(self, session_id: str):
        # The websocket to the browser verifies certificates with Python's own
        # store, which a python.org install on a Mac does not ship.
        try:
            import certifi  # noqa: PLC0415
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        except ImportError:
            pass
        self.session_id = session_id
        self.acts = 0
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                        name="stagehand")
        self._thread.start()
        self._sh = self._run(self._open(), 90)

    def _run(self, coro, seconds: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(seconds)

    async def _open(self):
        from stagehand import Stagehand, browserbase  # noqa: PLC0415
        browser = await browserbase.connect(api_key=os.environ["BROWSERBASE_API_KEY"],
                                            session_id=self.session_id)
        return await Stagehand.create(browser=browser, model=_generate,
                                      logging={"level": "off", "format": "json"})

    def act(self, instruction: str) -> tuple[bool, str]:
        """(done, what it did or why not). One action on the page."""
        self.acts += 1
        try:
            r = self._run(self._sh.act(instruction, timeout=ACT_SECONDS * 1000), ACT_SECONDS + 15)
            data = r.data
            return bool(data.success), (data.action_description or data.message or "")
        except Exception as exc:                      # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    def close(self) -> None:
        try:
            self._run(self._sh.close(), 20)
        except Exception:                             # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
