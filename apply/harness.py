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
import re
import sqlite3

import llm
from apply import confirm, pagecheck
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
        "name": "next_page",
        "description": ("Press Next/Continue on a form that goes on to another page, "
                        "after every required field on this page is filled. Returns "
                        "the next page's fields. Never use it to send: that is submit."),
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


def uncovered(browser: BaseBrowser, conn: sqlite3.Connection,
              question: str) -> list[str]:
    """Required boxes still empty that the model's question does not name.

    The person gets ONE list per page. Swiggy and Konovo (2026-10-01) each
    asked twice on the same page because the model's first question left
    boxes out, and a second round on a phone is where people drop off. Code
    adds what the model missed: required, empty, not a file, consent tick or
    free-text box (the model fills those itself), and not already in the vault.
    """
    import vault
    try:
        fields = browser.read_form()
        values = browser.current_values()
        known = vault.slice_for([f.get("label") or f["name"] for f in fields], conn)
    except Exception:                                  # noqa: BLE001
        return []
    asked = (question or "").lower()
    out = []
    for f in fields:
        label = (f.get("label") or f["name"]).strip()
        if not f.get("required") or values.get(f["name"]):
            continue
        if f.get("type") in ("file", "checkbox", "textarea") or label in known:
            continue
        if label.lower()[:40] in asked:
            continue
        opts = [str(o) for o in (f.get("options") or [])][:6]
        out.append(label + (f" ({' / '.join(opts)})" if opts else ""))
    return out


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


def _their_answers(history: list) -> str:
    """Everything the person typed in reply to this run's questions: the
    tool_results that closed an `ask_user`. Only these, never the model's
    own messages, so nothing it made up counts as said."""
    asked = set()
    for msg in history:
        for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "ask_user":
                asked.add(b.get("id"))
    out = []
    for msg in history:
        for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") in asked):
                out.append(str(b.get("content") or ""))
    return "\n".join(out)


_DATE_SAID = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?[\s,/.-]+(?:[A-Za-z]{3,9}|\d{1,2})[\s,/.-]+\d{4}\b"
    r"|\b\d{4}-\d{1,2}-\d{1,2}\b|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b")


def _same_date(value: str, said: str) -> str | None:
    """The date `value` names, as YYYY-MM-DD, when the person wrote that same
    day somewhere in their answers; else None."""
    from apply.cloud import _parse_date             # noqa: PLC0415
    want = _parse_date(value or "")
    if want is None:
        return None
    for m in _DATE_SAID.finditer(said or ""):
        text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", m.group(0))
        # "March 14, 1995" parses with its comma, "14 March, 1995" without.
        for variant in (text, text.replace(",", " ")):
            if _parse_date(re.sub(r"\s+", " ", variant).strip()) == want:
                return want.isoformat()
    return None


def remember_answer(conn: sqlite3.Connection, history: list,
                    args: dict) -> dict | None:
    """A form value the person gave us, kept for the next form.

    Sai's eval sheet row 27: "store important user information". Their
    notice period went into QuillBot's form and was asked for again on the
    next one, because answers only ever reached the form. Saved only when
    the field maps to a vault key AND the value is in their own reply, so a
    value the model derived or guessed is never stored as theirs."""
    import vault
    key = vault.match_key(args.get("name") or "")
    value = (args.get("value") or "").strip()
    if not key or not value or len(value) > 120:
        return None
    raw = _their_answers(history)
    said = vault._norm(raw)
    if not said:
        return None
    if vault._norm(value) not in said:
        # A date is the same answer in another format: Sai typed "14 march
        # 1995", the form took 1995-03-14, and it was never kept, so the next
        # form would ask again (Loop, 2026-10-01).
        value = _same_date(value, raw)
        if not value:
            return None
    # A country is never a city: "India" for "where are you based" went in
    # over "Bengaluru" and the next Swiggy form said City: India (2026-10-01).
    if key == "location" and vault.is_country(value):
        key = "country"
    if vault.get(key, conn) == value:
        return None
    vault.put(key, value, source="user", conn=conn)
    return {"key": key, "value": value}


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
        # A fresh start never carries the old run: Swiggy's retry (2026-09-30)
        # reused a finished log ending in "Stop and say so", and the model
        # stopped again without opening the form.
        history = load(conn, app_id) if first_message is None else None
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
        #: What the page said when it confirmed: the first line of the outcome,
        #: so the "sent" update quotes the site, not the model.
        proof = ""
        #: A click that changed the page without it saying "received". Kept
        #: apart from `blocked`: it may have been sent, so no retry.
        unconfirmed = False
        #: Submit refused because sending is not switched on for this person
        #: (SUBMIT_USERS): the outcome is written by code, see below.
        sending_off = False
        #: Whether this run pressed Submit at all. A run that never did and
        #: ends on a question is waiting for the person, not blocked.
        tried_submit = False

        from apply import worker
        for step in range(BUDGET):
            if worker.cancelled(app_id):
                trace(conn, app_id, step, "cancelled", {})
                return {"status": "cancelled", "submitted": False, "steps": step}
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
                status = ("submitted" if submitted else
                          "unconfirmed" if unconfirmed else "blocked")
                # The model's last line is what the person reads. Swiggy's
                # said "The application was submitted" over a run that sent
                # nothing; code has the last word on that.
                if status == "blocked" and pagecheck.claims_sent(text):
                    text = pagecheck.NOT_SENT
                # Questions written as its last words instead of an ask_user
                # call. Atlys PM ended "blocked" asking for expected CTC and a
                # joining date, so the answers had nowhere to go and the chat
                # told Sai it was blocked (2026-10-05). It is waiting.
                if status == "blocked" and not tried_submit and "?" in text:
                    save(conn, app_id, history, "waiting", text.strip())
                    trace(conn, app_id, step, "park", text.strip())
                    return {"status": "waiting", "question": text.strip(),
                            "steps": step}
                if status == "submitted" and proof:
                    text = f"{proof}\n\n{text}"
                save(conn, app_id, history, status)
                return {"status": status, "submitted": submitted,
                        "message": text.strip(), "steps": step}

            results = []
            for block in reply.content:
                if block.type != "tool_use":
                    continue
                if block.name == "ask_user" and isinstance(block.input, dict):
                    extra = uncovered(browser, conn, block.input.get("question", ""))
                    if extra:
                        block.input["question"] = (
                            block.input.get("question", "").rstrip()
                            + "\nAlso needed:\n" + "\n".join(f"- {e}" for e in extra))
                trace(conn, app_id, step, "tool_call",
                      {"name": block.name, "input": block.input})
                if block.name in ("submit", "next_page") and worker.cancelled(app_id):
                    return {"status": "cancelled", "submitted": False, "steps": step}
                try:
                    out, is_error = _run_tool(browser, block.name, block.input)
                except Park as park:
                    save(conn, app_id, history, "waiting", park.question)
                    trace(conn, app_id, step, "park", park.question)
                    return {"status": "waiting", "question": park.question,
                            "steps": step}
                # Only a submit the page confirmed counts. A REFUSED comes back
                # as is_error; an "ERROR: NOT SENT" comes back as plain text,
                # and used to count as sent because it was not is_error.
                if block.name == "fill_field" and not is_error:
                    learned = remember_answer(conn, history, block.input)
                    if learned:
                        trace(conn, app_id, step, "learned", learned)
                if block.name == "submit":
                    tried_submit = True
                if block.name in ("submit", "next_page") and is_error \
                        and confirm.SENDING_OFF in out:
                    sending_off = True
                if block.name in ("submit", "next_page") and not is_error:
                    if out.startswith("submitted"):
                        submitted = True
                        proof = out.split("— ", 1)[-1].strip()
                    elif out.startswith("unconfirmed"):
                        unconfirmed = True
                trace(conn, app_id, step, "tool_result", out[:2000])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out, "is_error": is_error})

            # ALL results, one message.
            history.append({"role": "user", "content": results})
            # Stopped by our own switch, not the site, and nothing on the page
            # can change that, so the run ends here and code says why. Left to
            # the model, two more steps told an invited user "the site refused
            # the submission for this account" (AccorHotel, 2026-10-01).
            if sending_off:
                save(conn, app_id, history, "blocked")
                return {"status": "blocked", "submitted": False,
                        "message": confirm.SENDING_OFF_MESSAGE, "steps": step}
            save(conn, app_id, history, "running")

        save(conn, app_id, history, "failed")
        return {"status": "failed", "detail": f"step budget ({BUDGET}) exhausted"}
    finally:
        if close_after:
            conn.close()
