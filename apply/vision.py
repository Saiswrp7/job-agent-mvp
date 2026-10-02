"""A look at the filled form before the person is asked to approve it.

The apply model (glm-5.3) reads text only: it never saw that Loop's Keka form
showed "Experience: Months", two date-picker dropdowns as questions, and a
greyed-out Submit (2026-10-01). glm-4.6v sees pictures, on the same GLM
account and key, but only through Zhipu's OpenAI-style endpoint. Through the
Anthropic-style endpoint the rest of the app uses, the image is dropped
without an error, and glm-4.5v then described a form it made up (probe,
2026-10-01). So this file calls that endpoint directly.

What it is for: a second opinion, never the gate. Its problems go back to
the agent once; after that they ride along on the approval as warnings the
person reads. A failed look (no key, timeout, an unreadable reply) is None
and never blocks a run.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re

import httpx

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("VISION_MODEL", "glm-4.6v")
#: Tall forms are scaled down to this height first: a 1280 x 9000 picture is
#: mostly white space and costs tokens for nothing.
MAX_HEIGHT = 3200

PROMPT = """This is a screenshot of a job application form that software has
filled in. Below is what each box should hold.

{answers}

Look at the screenshot and list only real problems:
- a box from the list that is empty on the screen, or shows something different
- a red error message on the page
- a box marked required (*) that is still empty
Do not list boxes that look right. Do not guess about boxes you cannot see.
Reply with JSON only: {{"problems": ["<box>: <what is wrong>"]}}.
If everything looks right, reply {{"problems": []}}."""


def _shrink(image: bytes) -> tuple[bytes, str]:
    """JPEG, at most MAX_HEIGHT tall. The original if Pillow can't read it."""
    try:
        from PIL import Image                         # noqa: PLC0415
        im = Image.open(io.BytesIO(image)).convert("RGB")
        if im.height > MAX_HEIGHT:
            w = max(1, int(im.width * MAX_HEIGHT / im.height))
            im = im.resize((w, MAX_HEIGHT))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=70)
        return out.getvalue(), "image/jpeg"
    except Exception:                                 # noqa: BLE001
        return image, "image/png"


def _problems(text: str) -> list[str] | None:
    """The problems list from a reply that may wrap its JSON in prose or a
    code fence. None when there is no such list."""
    for m in re.finditer(r"\{[^{}]*\"problems\"\s*:\s*\[.*?\][^{}]*\}", text or "", re.S):
        try:
            got = json.loads(m.group(0)).get("problems")
        except (ValueError, AttributeError):
            continue
        if isinstance(got, list):
            return [str(p).strip() for p in got if str(p).strip()][:10]
    return None


def look(image: bytes, answers: dict[str, str], timeout: float = 90) -> list[str] | None:
    """Problems the picture shows, [] when it looks right, None when the look
    could not be had."""
    key = os.environ.get("GLM_API_KEY")
    if not key or not image or not answers:
        return None
    data, media = _shrink(image)
    listed = "\n".join(f"- {k}: {str(v)[:120]}" for k, v in answers.items())
    body = {"model": MODEL, "max_tokens": 1200, "temperature": 0.1,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:{media};base64,{base64.b64encode(data).decode()}"}},
                {"type": "text", "text": PROMPT.format(answers=listed)}]}]}
    try:
        r = httpx.post(URL, json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            print(f"[vision] {MODEL} answered {r.status_code}: {r.text[:200]}")
            return None
        reply = r.json()
    except Exception as exc:                          # noqa: BLE001
        print(f"[vision] look failed: {type(exc).__name__}: {exc}")
        return None
    try:
        import usage                                  # noqa: PLC0415
        usage.record(reply, model=MODEL, purpose="apply_look")
    except Exception:                                 # noqa: BLE001
        pass
    msg = ((reply.get("choices") or [{}])[0].get("message") or {})
    return _problems(msg.get("content") or "")
