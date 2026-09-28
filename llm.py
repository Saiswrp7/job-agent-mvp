"""One LLM entry point, with a provider that can be swapped.

Anthropic is the default and the shape everything is written against. GLM and
OpenRouter are here because on this machine both exported keys were dead at
build time, and a system that can only run on one provider is a system that
sometimes cannot run at all.

GLM costs nothing extra to support: Zhipu publishes an Anthropic-compatible
endpoint, so it is the same SDK with a different base_url and model. OpenRouter
speaks the OpenAI shape, so it gets a small translation.

`python cli.py doctor` probes every configured key and reports which one
actually answers. Probe before trusting a key — an exported key that 401s looks
exactly like a working one until you call it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
ENV_FILE = HERE / ".env"


def _load_env(path: Path = ENV_FILE) -> None:
    """Read KEY=value from .env, without overwriting a real environment value.

    Keeps secrets out of shell history and out of any transcript. .env is
    gitignored; it is the only place in this project a key should ever live.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

#: provider -> (env var, base_url or None, default model, cheap model)
PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY", None, "claude-opus-5", "claude-haiku-4-5"),
    # One generation for everything, so a change in behaviour is the code and
    # never a model swap: glm-5.3 for chat, ranking and apply; glm-5.3-flash
    # for the cheap slot (labels, probe). Both *always* think — disabling it
    # is a 400 (code 1210) — but `output_config.effort = "low"` skips the
    # thinking block entirely: 16 output tokens instead of ~200 on a label.
    "glm": ("GLM_API_KEY", "https://open.bigmodel.cn/api/anthropic",
            "glm-5.3", "glm-5.3-flash"),
    # The cheap slot is what `probe` calls, so it must be a model that answers
    # in a handful of tokens. A reasoning model cannot: it spends the whole
    # budget thinking and returns content: null, which reads as a dead key.
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                   "anthropic/claude-sonnet-5", "google/gemini-2.5-flash-lite"),
}

#: One client per provider. Keyed, not a single slot, because labelling runs on
#: a cheap provider while chat stays on the one with tool calling.
_clients: dict[str, object] = {}


def configured() -> list[str]:
    return [p for p, (env, *_) in PROVIDERS.items() if os.environ.get(env)]


def provider() -> str:
    """Which provider to use. `LLM_PROVIDER` wins; otherwise first configured."""
    forced = os.environ.get("LLM_PROVIDER")
    if forced:
        if forced not in PROVIDERS:
            raise ValueError(f"unknown LLM_PROVIDER {forced!r}; "
                             f"pick one of {list(PROVIDERS)}")
        return forced
    found = configured()
    if not found:
        raise RuntimeError(
            "No LLM key set. Export one of: "
            + ", ".join(env for env, *_ in PROVIDERS.values())
        )
    return found[0]


def models() -> tuple[str, str]:
    """(main, cheap) for the active provider."""
    _, _, main, cheap = PROVIDERS[provider()]
    return os.environ.get("LLM_MODEL", main), os.environ.get("LLM_MODEL_CHEAP", cheap)


def MODEL() -> str:
    return models()[0]


def RANK_MODEL() -> str:
    #: Split out so the expensive call can be moved to a cheaper model in one
    #: place. Test it against the CRM-trap cases first — spotting a lifecycle
    #: job wearing a growth title is the subtlest judgment in the system.
    return os.environ.get("RANK_MODEL", models()[0])


def client(name: str | None = None):
    p = name or provider()
    if p not in _clients:
        env, base_url, *_ = PROVIDERS[p]
        key = os.environ[env]
        if p == "openrouter":
            _clients[p] = _OpenAIish(key, base_url)
        else:
            import anthropic
            _clients[p] = (anthropic.Anthropic(api_key=key, base_url=base_url)
                           if base_url else anthropic.Anthropic(api_key=key))
    return _clients[p]


class _OpenAIish:
    """Minimal OpenAI-shaped client for OpenRouter.

    Only what this codebase uses: one system block, one user message, text
    back. Tool calling is deliberately absent — the apply agent needs real tool
    use, so it stays on an Anthropic-shaped provider.
    """

    def __init__(self, key: str, base_url: str):
        import httpx
        self._http = httpx.Client(
            base_url=base_url, timeout=180.0,
            headers={"Authorization": f"Bearer {key}",
                     "content-type": "application/json"},
        )

    def text(self, *, model: str, system: str, user: str, max_tokens: int,
             temperature: float | None = None) -> str:
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if temperature is not None:
            body["temperature"] = temperature
        r = self._http.post("/chat/completions", json=body)
        r.raise_for_status()
        import usage
        usage.record(r.json(), model=model)
        choice = r.json()["choices"][0]
        content = choice["message"].get("content")
        if not content:
            # A reasoning model that hit the ceiling returns content: null with
            # the tokens spent in `reasoning`. Say that, rather than letting a
            # None reach .strip() and surface as an AttributeError.
            raise RuntimeError(
                f"{model} returned no content (finish_reason="
                f"{choice.get('finish_reason')!r}); raise max_tokens or use a "
                f"model that does not reason."
            )
        return content


#: Every request this process has made. A chat turn reports its own cost by
#: sampling this before and after, which is the only way to count the calls
#: that happen *inside* a tool — `search_jobs` alone is two more, and a turn
#: that reports its own loop count under-reports the bill by half.
_CALLS = 0


def counted() -> int:
    """Called immediately before each request. Returns the new total."""
    global _CALLS
    _CALLS += 1
    return _CALLS


def calls() -> int:
    return _CALLS


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def effort_level(level: str, via: str | None = None) -> str:
    """The effort level this provider accepts. GLM 5.3 takes low, high or max
    and rejects medium with a 400 (code 1210), which broke every chat turn the
    day the model was switched. Medium rounds up: the chat is the product."""
    if (via or provider()) == "glm" and level == "medium":
        return "high"
    return level


def supports_tools() -> bool:
    """The apply agent needs real tool use, which the OpenAI shim does not do."""
    return provider() in ("anthropic", "glm")


def complete(system: str, user: str, *, model: str | None = None,
             max_tokens: int = 4096, effort: str = "high",
             cache: bool = True, ttl: str = "1h",
             via: str | None = None,
             temperature: float | None = None,
             timeout: float | None = None) -> str:
    """`via` names a provider for this one call, leaving `LLM_PROVIDER` alone.
    Labelling uses it: a cheap classifier should not need the provider that
    the agents need for tool calling.

    `temperature` reaches OpenRouter and GLM. On Anthropic it is ignored:
    adaptive thinking there fixes it, and sending one is an error.
    """
    p = via or provider()
    model = model or MODEL()
    c = client(p)

    if isinstance(c, _OpenAIish):
        counted()
        return c.text(model=model, system=system, user=user,
                      max_tokens=max_tokens, temperature=temperature).strip()

    system_blocks = [{"type": "text", "text": system}]
    if cache and p == "anthropic":
        system_blocks[0]["cache_control"] = {"type": "ephemeral", "ttl": ttl}

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user}],
    }
    if p == "anthropic":
        # Adaptive thinking; budget_tokens is a 400 on Opus 5.
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}
    elif p == "glm":
        # GLM 5.3 reads the same field. "low" answers with no thinking block,
        # which is what a form-fill or a label wants; "high" thinks first.
        kwargs["output_config"] = {"effort": effort_level(effort, p)}
        if temperature is not None:
            kwargs["temperature"] = temperature

    if timeout is not None:
        # Per request. The client's own limit is 180 s with two retries, so
        # one stuck reply could hold a resume upload for 7 minutes (414 s seen).
        kwargs["timeout"] = timeout
    counted()
    resp = c.messages.create(**kwargs)
    import usage
    usage.record(resp, model=model)
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(f"refused: {getattr(resp, 'stop_details', None)}")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def complete_json(system: str, user: str, **kw) -> dict | list:
    """Parse JSON out of the reply.

    Tolerates a fenced block and leading prose, because a model asked for JSON
    sometimes wraps it, and failing a whole search over a code fence is silly.
    """
    raw = complete(system, user, **kw)
    m = _FENCE.search(raw)
    if m:
        raw = m.group(1)
    starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    if starts:
        raw = raw[min(starts):]
    try:
        # raw_decode, not loads: it reads the first complete JSON value and
        # stops, so a model that adds a sentence after the array is fine too.
        value, _ = json.JSONDecoder().raw_decode(raw)
        return value
    except json.JSONDecodeError as exc:
        # A long answer usually breaks on one unescaped quote inside a string.
        # Handing it back is cheaper and more reliable than regex surgery, and
        # it costs nothing on the normal path because it only runs on failure.
        # Once, though: if the repair is also malformed, the model is confused
        # about the shape and a third try will not help.
        if kw.pop("_repairing", False):
            raise ValueError(f"model did not return JSON: {raw[:300]!r}") from exc
        repaired = complete(
            "You fix malformed JSON. Return the corrected JSON and nothing "
            "else — no explanation, no code fence. Change only what makes it "
            "invalid, usually an unescaped double quote inside a string. "
            "Never drop or reword any content.",
            f"Invalid JSON ({exc}):\n\n{raw}",
            **{**kw, "max_tokens": kw.get("max_tokens", 4096)},
        )
        try:
            value, _ = json.JSONDecoder().raw_decode(repaired.strip())
            return value
        except json.JSONDecodeError:
            raise ValueError(
                f"model did not return JSON, and the repair failed too: "
                f"{raw[:300]!r}") from exc


def probe(name: str) -> tuple[bool, str]:
    """Call a provider for real. Returns (ok, detail)."""
    env, base_url, main, cheap = PROVIDERS[name]
    if not os.environ.get(env):
        return False, f"{env} not set"
    try:
        # effort="low": a thinking model spends a small budget thinking and
        # returns empty text, which reads as a live key that says nothing.
        out = complete("Reply with exactly: ok", "ping", model=cheap,
                       max_tokens=256, cache=False, via=name, effort="low")
        if not out:
            return True, f"{cheap} answered, but with empty text"
        return True, f"{cheap} -> {out[:40]!r}"
    except Exception as exc:                          # noqa: BLE001
        detail = str(exc)
        m = re.search(r"\b(401|403|404|429|5\d\d)\b", detail)
        return False, f"{type(exc).__name__}" + (f" {m.group(1)}" if m else "")
    finally:
        # A probe that built a client for a dead key should not leave it
        # cached for the real calls that follow.
        _clients.pop(name, None)
