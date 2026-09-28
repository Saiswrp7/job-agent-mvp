"""Count the tokens every model call spends, without touching product code.

`llm.py` does not keep the usage the provider sends back, so the launch evals
wrap the SDK's `Messages.create` for the life of the eval process only. Every
call the product makes (filters, rank, tailor, parse, the chat loop) goes
through that one method, so nothing is missed and nothing is changed.

Prices are not in the code base and GLM's reply carries none, so this module
reports tokens only. A cost figure would be a guess.
"""

from __future__ import annotations

import time

CALLS: list[dict] = []
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    from anthropic.resources import messages as M

    original = M.Messages.create

    def create(self, *args, **kwargs):
        t0 = time.monotonic()
        resp = original(self, *args, **kwargs)
        u = getattr(resp, "usage", None)
        CALLS.append({
            "model": kwargs.get("model"),
            "input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "seconds": round(time.monotonic() - t0, 1),
        })
        return resp

    M.Messages.create = create
    _installed = True


def mark() -> int:
    return len(CALLS)


def since(start: int) -> dict:
    rows = CALLS[start:]
    return {"calls": len(rows),
            "input": sum(r["input"] for r in rows),
            "output": sum(r["output"] for r in rows),
            "cache_read": sum(r["cache_read"] for r in rows),
            "total": sum(r["input"] + r["output"] + r["cache_read"] for r in rows)}
