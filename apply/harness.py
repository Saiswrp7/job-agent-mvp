"""The agent loop, and the machinery that lets it park.

The loop is twelve lines. Everything else here is the machinery: a step budget
so it cannot spin, a message log in SQLite so it can stop mid-form and resume
hours later, and a trace of every prompt and result so a 2am failure is
debuggable at all.

Three things in the loop are load-bearing and easy to get wrong:

- Append `reply.content`, not its text. Extracting the string drops thinking
  blocks and breaks the following turn.
- Return every tool_result in ONE user message. Splitting them across messages
  trains the model out of parallel tool calls.
- A throwing tool returns `is_error: True`, it does not crash the run. The
  model recovers from an error it can read.
"""

from __future__ import annotations

import json
import sqlite3

import llm
from apply.browser import BaseBrowser, Park, SubmitRefused
from engine import db

BUDGET = 40

TOOLS = [
    {
        "name": "read_form",
        "description": "Read every field on the application form. Call this first.",
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
    {
        "name": "fill_field",
        "description": "Type a value into one form field.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "value": {"type": "string"}},
            "required": ["name", "value"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "upload_file",
        "description": "Attach a file to a file-upload field.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "path": {"type": "string"}},
            "required": ["name", "path"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "ask_user",
        "description": ("Ask the person one question. Suspends the run until "
                        "they answer, so put EVERY gap in a single question."),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "submit",
        "description": "Submit the application. Only when every required field is filled.",
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
    {
        "name": "screenshot",
        "description": "Capture the current page.",
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
]


def trace(conn: sqlite3.Connection, app_id: int, step: int, kind: str,
          payload) -> None:
    conn.execute(
        "INSERT INTO agent_logs (app_id, step, kind, payload) VALUES (?,?,?,?)",
        (app_id, step, kind,
         payload if isinstance(payload, str) else json.dumps(payload, default=str)),
    )
    conn.commit()


def _run_tool(browser: BaseBrowser, name: str, args: dict) -> tuple[str, bool]:
    """(result_text, is_error). Park propagates — it is not an error."""
    try:
        fn = getattr(browser, name)
        out = fn(**args)
        if name == "read_form":
            return json.dumps(out), False
        return str(out), False
    except Park:
        raise
    except SubmitRefused as exc:
        return f"REFUSED: {exc}", True
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}", True
    except Exception as exc:                          # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}", True


def _block(obj):
    """A reply block as the API takes it back.

    `default=str` wrote SDK blocks as their repr — "ThinkingBlock(...)" — which
    reloads as a string where the API needs an object. Nothing noticed while
    the provider rarely returned thinking blocks; GLM 5.3 always does, so the
    first resumed application after the switch was a 400 (2026-09-24).
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return str(obj)


def save(conn: sqlite3.Connection, app_id: int, history: list,
         status: str, question: str | None = None) -> None:
    conn.execute(
        "UPDATE applications SET log=?, status=?, question=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(history, default=_block), status, question, app_id),
    )
    conn.commit()


def load(conn: sqlite3.Connection, app_id: int) -> list | None:
    row = conn.execute("SELECT log FROM applications WHERE id=?",
                       (app_id,)).fetchone()
    return json.loads(row["log"]) if row and row["log"] else None


def run(app_id: int, browser: BaseBrowser, system: str,
        first_message: str | None = None,
        answer: str | None = None,
        conn: sqlite3.Connection | None = None) -> dict:
    """Run or resume one application.

    Fresh start: pass `first_message`.
    Resuming a parked run: pass `answer`, and the saved log is reloaded.
    """
    close_after = conn is None
    conn = conn or db.connect()
    try:
        history = load(conn, app_id)
        if history is None:
            history = [{"role": "user", "content": first_message or "Begin."}]
        elif answer is not None:
            # The parked ask_user call is still the last tool_use; close it with
            # the user's answer so the model resumes exactly where it stopped.
            last = history[-1]
            pending = next(
                (b for b in (last.get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "tool_use"),
                None,
            )
            if pending:
                history.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": pending["id"],
                    "content": answer,
                }]})
            else:
                history.append({"role": "user", "content": answer})

        client = llm.client()
        save(conn, app_id, history, "running")

        # Did `submit` actually go through? Nothing used to record this, so the
        # run ended as `done` whether it had submitted or been refused, and
        # "done" reads as success to whatever displays it next. It reached a
        # person as "all five are submitted" when nothing had been.
        submitted = False

        for step in range(BUDGET):
            trace(conn, app_id, step, "prompt", {"messages": len(history)})
            llm.counted()      # this loop calls create() directly, not via llm
            reply = client.messages.create(
                model=llm.MODEL(),
                max_tokens=8192,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=history,
                thinking={"type": "adaptive"},
                output_config={"effort": llm.effort_level("high")},
            )
            import usage
            usage.record(reply, model=llm.MODEL(), purpose="apply")

            if reply.stop_reason == "refusal":
                save(conn, app_id, history, "failed")
                return {"status": "failed",
                        "detail": str(getattr(reply, "stop_details", "refused"))}

            # The whole content block, never just the text.
            history.append({"role": "assistant", "content": reply.content})

            if reply.stop_reason != "tool_use":
                text = "".join(b.text for b in reply.content if b.type == "text")
                # Named for what happened, never for the loop. `blocked` means
                # the run reached its end without submitting — the ordinary
                # outcome while ALLOW_SUBMIT is off, and not a failure.
                status = "submitted" if submitted else "blocked"
                save(conn, app_id, history, status)
                return {"status": status, "submitted": submitted,
                        "message": text.strip(), "steps": step}

            results = []
            for block in reply.content:
                if block.type != "tool_use":
                    continue
                trace(conn, app_id, step, "tool_call",
                      {"name": block.name, "input": block.input})
                try:
                    out, is_error = _run_tool(browser, block.name, block.input)
                except Park as park:
                    save(conn, app_id, history, "waiting", park.question)
                    trace(conn, app_id, step, "park", park.question)
                    return {"status": "waiting", "question": park.question,
                            "steps": step}
                # Only a submit that returned without erroring counts. A
                # REFUSED comes back as is_error, so the flag stays false and
                # the run ends `blocked`.
                if block.name == "submit" and not is_error:
                    submitted = True
                trace(conn, app_id, step, "tool_result", out[:2000])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out, "is_error": is_error})

            # ALL results, one message.
            history.append({"role": "user", "content": results})
            save(conn, app_id, history, "running")

        save(conn, app_id, history, "failed")
        return {"status": "failed", "detail": f"step budget ({BUDGET}) exhausted"}
    finally:
        if close_after:
            conn.close()
