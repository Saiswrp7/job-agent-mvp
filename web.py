"""Browser chat — the same agent, in a page instead of a terminal.

This is a surface, not an engine. Every message goes to `chat.reply()`, the
exact function the REPL calls, so the browser can never drift from the terminal
or grow behaviour of its own. If something is wrong here it is wrong in both.

Standard library only, and bound to 127.0.0.1. Nothing installs, nothing
listens on the network, and the sqlite connection stays on one thread because
`HTTPServer` handles requests in the thread that called `serve_forever`.
"""

from __future__ import annotations

import json
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import chat
import llm
import paths
from engine import db

HERE = Path(__file__).resolve().parent
PORT = 8765

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Agent</title>
<style>
  :root {
    --bg:#fbfaf9; --panel:#fff; --ink:#1a1a19; --muted:#6b6963;
    --line:#e7e4df; --accent:#3d5a3d; --mine:#eef1ee; --err:#a33;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#17181a; --panel:#1f2023; --ink:#e9e8e6; --muted:#9a9894;
      --line:#2e3034; --accent:#8fb58f; --mine:#26292a; --err:#e88;
    }
  }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
    display:flex; flex-direction:column;
  }
  header {
    padding:14px 16px; border-bottom:1px solid var(--line);
    display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
    background:var(--panel);
  }
  header b { font-size:15px; font-weight:600; letter-spacing:-.01em; }
  header span { color:var(--muted); font-size:12.5px; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--accent);
         display:inline-block; margin-right:5px; }
  main {
    flex:1; overflow-y:auto; padding:22px 16px;
    display:flex; flex-direction:column; gap:18px;
  }
  .wrap { width:100%; max-width:720px; margin:0 auto; }
  .turn { display:flex; flex-direction:column; gap:18px; }
  .msg { max-width:100%; }
  .msg.me .bubble {
    background:var(--mine); border:1px solid var(--line);
    border-radius:14px; padding:10px 14px; display:inline-block;
    max-width:85%; white-space:pre-wrap;
  }
  .msg.me { align-items:flex-end; display:flex; flex-direction:column; }
  .msg.bot .bubble { padding:0; }
  .msg.bot.error .bubble { color:var(--err); }
  .bubble a { color:var(--accent); text-decoration:none;
              border-bottom:1px solid color-mix(in srgb,var(--accent) 35%,transparent); }
  .bubble a:hover { border-bottom-color:var(--accent); }
  .bubble p { margin:0 0 .7em; }
  .bubble p:last-child { margin-bottom:0; }
  .bubble .job { margin:0 0 1.1em; padding-left:1.6em; text-indent:-1.6em; }
  .bubble strong { font-weight:600; }
  .think { color:var(--muted); font-style:italic; }
  .think::after {
    content:"·"; animation:d 1.2s steps(4,end) infinite; margin-left:2px;
  }
  @keyframes d { 0%{content:"·"} 25%{content:"··"} 50%{content:"···"} 75%{content:""} }
  footer { border-top:1px solid var(--line); background:var(--panel); padding:12px 16px; }
  form { display:flex; gap:9px; align-items:flex-end; }
  textarea {
    flex:1; resize:none; font:inherit; color:inherit; background:var(--bg);
    border:1px solid var(--line); border-radius:11px; padding:10px 12px;
    max-height:160px; min-height:44px;
  }
  textarea:focus { outline:2px solid color-mix(in srgb,var(--accent) 45%,transparent);
                   outline-offset:-1px; }
  button {
    font:inherit; font-weight:550; border:0; border-radius:11px; padding:0 18px;
    height:44px; background:var(--accent); color:var(--panel); cursor:pointer;
  }
  button:disabled { opacity:.45; cursor:default; }
  .hint { color:var(--muted); font-size:12px; margin-top:7px; }
  #clip {
    height:44px; width:42px; flex:0 0 42px; border:1px solid var(--line);
    border-radius:11px; display:flex; align-items:center; justify-content:center;
    color:var(--muted); cursor:pointer; background:var(--bg);
  }
  #clip:hover { color:var(--accent); border-color:var(--accent); }
  #drop {
    position:fixed; inset:0; background:color-mix(in srgb,var(--bg) 88%,transparent);
    display:none; align-items:center; justify-content:center; z-index:9;
    font-weight:550; color:var(--accent); pointer-events:none;
  }
  #drop.on { display:flex; }
  #drop div { border:2px dashed var(--accent); border-radius:16px; padding:28px 40px; }

  /* --- the two panes -----------------------------------------------------
     The conversation is what you read; the work is what you check. Merging
     them buries the answer under the machinery, so they get their own
     columns and only stack when the screen is too narrow for two. */
  .split { flex:1; display:flex; min-height:0; }
  .pane-chat { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  #work {
    flex:0 0 330px; border-left:1px solid var(--line); background:var(--panel);
    overflow-y:auto; padding:14px 16px 24px; font-size:13px;
  }
  #work h2 {
    font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    letter-spacing:.11em; text-transform:uppercase; color:var(--muted);
    margin:0 0 4px; position:sticky; top:-14px; background:var(--panel);
    padding:14px 0 9px;
  }
  #work .idle { color:var(--muted); font-size:12.5px; line-height:1.55; }
  .job-turn { border-top:1px solid var(--line); padding:12px 0 4px; }
  .job-turn:first-of-type { border-top:0; }
  .job-turn .asked {
    color:var(--muted); font-size:12px; margin-bottom:8px;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
    overflow:hidden;
  }
  .step { margin:0 0 7px; line-height:1.45; }
  .step .what::before {
    content:"●"; color:var(--accent); font-size:9px;
    margin-right:7px; vertical-align:2px;
  }
  .step.think .what { color:var(--muted); font-style:italic; }
  .step.think .what::before { content:"○"; color:var(--muted); }
  .step.fail .what::before { content:"▲"; color:var(--err); }
  .funnel { margin:6px 0 2px 16px; }
  .funnel .lbl {
    display:block; color:var(--muted); font-size:11px; letter-spacing:.06em;
    text-transform:uppercase; margin:7px 0 2px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  .funnel .v {
    display:block; padding-left:9px; border-left:1px solid var(--line);
    font-size:12px; line-height:1.5;
  }
  .funnel .counts {
    display:block; margin:8px 0 0; font-size:12.5px; font-weight:550;
    font-variant-numeric:tabular-nums;
  }
  .funnel .flat { display:block; color:var(--err); font-size:11.5px; margin-top:3px; }
  .funnel .cut { display:block; padding-left:9px; border-left:1px solid var(--line);
                 font-size:12px; line-height:1.45; margin-top:4px; }
  .funnel .cut b { font-weight:550; }
  .funnel .cut i { font-style:normal; color:var(--muted); display:block; }
  .step .files { display:block; margin:4px 0 0 16px; }
  .step .fp {
    display:block; color:var(--ink); opacity:.78; margin-top:5px;
    font:11.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  .step .fw {
    display:block; color:var(--muted); font-size:11.5px; line-height:1.45;
    padding-left:9px; border-left:1px solid var(--line); margin:1px 0 0 1px;
  }
  .step .ms { color:var(--muted); font-size:11.5px; margin-left:7px; }
  .job-turn .cost {
    color:var(--muted); font-size:11.5px; margin:8px 0 0 16px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  @media (max-width:820px) {
    .split { flex-direction:column; }
    #work { flex:0 0 auto; max-height:34vh; border-left:0;
            border-top:1px solid var(--line); }
    #work h2 { top:-14px; }
  }
</style>
</head>
<body>
<header>
  <b>Job Agent</b>
  <span><span class="dot"></span>__PROVIDER__</span>
  <span id="loaded">__LOADED__</span>
</header>
<div id="drop"><div>Drop your resume to read it</div></div>
<div class="split">
<div class="pane-chat">
<main><div class="wrap turn" id="log"></div></main>
<footer>
  <div class="wrap">
    <form id="f">
      <label id="clip" title="Attach your resume (PDF)">
        <input type="file" id="file" accept="application/pdf,.pdf" hidden>
        <svg viewBox="0 0 24 24" width="19" height="19" fill="none"
             stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <path d="M21 11l-8.5 8.5a5 5 0 01-7-7L14 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L15 6"/>
        </svg>
      </label>
      <textarea id="m" rows="1" placeholder="growth roles in Bangalore, nothing CRM-heavy&#10;&#10;Enter to send · Shift+Enter for a new line" autofocus></textarea>
      <button id="b">Send</button>
    </form>
    <div class="hint">Attach a PDF resume, or drop one anywhere. Applications never submit: ALLOW_SUBMIT=0.</div>
  </div>
</footer>
</div>
<aside id="work">
  <h2>What it's doing</h2>
  <div class="idle" id="work-idle">
    Every action shows here as it happens, with the files it ran.
    <br><br>An answer with nothing listed above it was written without
    checking anything.
  </div>
</aside>
</div>
<script>
const log=document.getElementById('log'), f=document.getElementById('f'),
      m=document.getElementById('m'), b=document.getElementById('b'),
      work=document.getElementById('work');

// Everything that reaches innerHTML goes through here first. Job titles and
// tool results are other people's text, so they are data, never markup.
function esc(s){
  return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

// The agent answers in light markdown. Escape first, then re-introduce only
// the three things it actually emits, so a job description can never inject.
function inline(s){
  return s
    .replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')
    .replace(/(https?:\\/\\/[^\\s<]+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>');
}
const NUMBERED=/^\\s*\\d+\\./;
function render(s){
  return esc(s).split(/\\n\\s*\\n/).map(para=>{
    const lines=para.split('\\n');
    if(!NUMBERED.test(lines[0])) return `<p>${inline(lines.join('<br>'))}</p>`;
    // Each "1." starts its own block; the lines under it are its continuation.
    // One paragraph per item is what makes the hanging indent line up — a
    // <br> inside one paragraph would keep the padding but lose the outdent.
    const items=[];
    for(const ln of lines){
      if(NUMBERED.test(ln)||!items.length) items.push([ln.trim()]);
      else items[items.length-1].push(ln.trim());
    }
    return items.map(it=>`<p class="job">${inline(it.join('<br>'))}</p>`).join('');
  }).join('');
}
function add(who,html,cls=''){
  const d=document.createElement('div');
  d.className=`msg ${who} ${cls}`;
  d.innerHTML=`<div class="bubble">${html}</div>`;
  log.appendChild(d);
  d.scrollIntoView({behavior:'smooth',block:'end'});
  return d;
}
async function upload(file){
  if(!file) return;
  b.disabled=true;
  add('me',`<strong>${file.name}</strong>`);
  const waiting=add('bot','<span class="think">reading it</span>');
  try{
    const data=await new Promise((ok,no)=>{
      const r=new FileReader();
      r.onload=()=>ok(r.result.split(',')[1]); r.onerror=no;
      r.readAsDataURL(file);
    });
    const res=await fetch('/api/upload',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:file.name,data})});
    const d=await res.json();
    waiting.remove();
    if(d.error) add('bot',render(d.error),'error'); else add('bot',render(d.reply));
  }catch(err){ waiting.remove(); add('bot',render('Upload failed: '+err.message),'error'); }
  b.disabled=false; m.focus();
}
document.getElementById('file').addEventListener('change',e=>{
  upload(e.target.files[0]); e.target.value='';
});
const drop=document.getElementById('drop');
let depth=0;
addEventListener('dragenter',e=>{e.preventDefault(); if(++depth) drop.classList.add('on');});
addEventListener('dragleave',()=>{ if(--depth<=0){depth=0; drop.classList.remove('on');}});
addEventListener('dragover',e=>e.preventDefault());
addEventListener('drop',e=>{
  e.preventDefault(); depth=0; drop.classList.remove('on');
  upload(e.dataTransfer.files[0]);
});
m.addEventListener('input',()=>{m.style.height='auto';m.style.height=m.scrollHeight+'px';});
m.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();f.requestSubmit();}
});
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const text=m.value.trim(); if(!text) return;
  m.value=''; m.style.height='auto'; b.disabled=true;
  add('me',render(text));
  const waiting=add('bot','<span class="think">thinking</span>');

  // The work goes in its own column, never into the conversation. It stays
  // there after the answer lands, so the two panes read as transcript and
  // receipt rather than one merged stream.
  const idle=document.getElementById('work-idle'); if(idle) idle.remove();
  const live=[], panel=document.createElement('div');
  panel.className='job-turn';
  panel.innerHTML=`<div class="asked">${esc(text)}</div><div class="steps"></div>`;
  work.appendChild(panel);
  const t0=Date.now();
  // What the search narrowed, and what it threw away. Five jobs look equally
  // confident whether the other fifteen were junk or whether one of them was
  // the one they wanted.
  function funnelHtml(fn){
    if(!fn) return '';
    let h='<span class="funnel">';
    if(fn.looked_for?.length)
      h+='<span class="lbl">looked for</span>'+
         fn.looked_for.map(x=>'<span class="v">'+esc(x)+'</span>').join('');
    if(fn.relaxed?.length)
      h+='<span class="lbl">loosened</span>'+
         fn.relaxed.map(x=>'<span class="v">'+esc(x)+'</span>').join('');
    h+='<span class="counts">'+fn.open+' open &rarr; '+fn.matched+
       ' matched &rarr; '+fn.shown+' shown</span>';
    if(fn.matched&&fn.matched===fn.shown)
      h+='<span class="flat">nothing was filtered out &mdash; '+
         'the AI had no choice to make</span>';
    if(fn.dropped?.length)
      h+='<span class="lbl">not shown</span>'+fn.dropped.slice(0,8).map(d=>
        '<span class="cut"><b>'+esc(d.title)+'</b> &middot; '+esc(d.company)+
        (d.why?'<i>'+esc(d.why)+'</i>':'')+'</span>').join('');
    return h+'</span>';
  }
  function paint(){
    panel.querySelector('.steps').innerHTML=live.map(l=>
      `<div class="step ${l.state}"><span class="what">${esc(l.text)}${
        l.time?'<span class="ms">'+l.time+'</span>':''
      }</span>${funnelHtml(l.funnel)}${
        l.files?.length
          ? '<span class="files">'+l.files.map(f=>{
              const p=Array.isArray(f)?f[0]:f, why=Array.isArray(f)?f[1]:'';
              return '<span class="fp">'+esc(p)+'</span>'+
                     (why?'<span class="fw">'+esc(why)+'</span>':'');
            }).join('')+'</span>' : ''
      }</div>`).join('');
    work.scrollTop=work.scrollHeight;
  }
  function cost(calls){
    const el=document.createElement('div');
    el.className='cost';
    el.textContent=((Date.now()-t0)/1000).toFixed(1)+'s'+
      (live.some(l=>l.state!=='think')?'':' · nothing was checked');
    panel.appendChild(el);
  }
  function event(ev){
    if(ev.kind==='thinking'){
      if(!live.length||live[live.length-1].state!=='think')
        live.push({text:'thinking',state:'think'});
      paint();
    } else if(ev.kind==='tool'){
      if(live.length&&live[live.length-1].state==='think') live.pop();
      live.push({text:ev.text,files:ev.files,state:'run'});
      paint();
    } else if(ev.kind==='tool_done'){
      const last=live[live.length-1];
      if(last){ last.state=ev.ok?'done':'fail';
                last.time=(ev.ms/1000).toFixed(1)+'s';
                if(ev.funnel) last.funnel=ev.funnel; }
      paint();
    }
  }
  try{
    const r=await fetch('/api/chat',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({message:text})});
    const reader=r.body.getReader(), dec=new TextDecoder();
    let buf='', finished=false;
    for(;;){
      const {done,value}=await reader.read();
      if(done) break;
      buf+=dec.decode(value,{stream:true});
      // '\\n' not '\\n': PAGE is an ordinary Python string, so a single
      // backslash-n here would reach the browser as a real newline inside a
      // JS string literal, which is a syntax error that kills the whole file.
      const lines=buf.split('\\n'); buf=lines.pop();
      for(const ln of lines){
        if(!ln.trim()) continue;
        const ev=JSON.parse(ln);
        if(ev.kind==='reply'||ev.kind==='error'){
          finished=true; waiting.remove();
          // Drop a trailing "thinking" so the pane ends on what it actually
          // did, not on the last thing it was about to do.
          if(live.length&&live[live.length-1].state==='think') live.pop();
          paint(); cost();
          add('bot',render(ev.text),ev.kind==='error'?'error':'');
          if(ev.loaded) document.getElementById('loaded').textContent=ev.loaded;
        } else event(ev);
      }
    }
    // The stream ended without an answer: the server died mid-turn. Say so
    // rather than leaving the work list sitting there looking busy forever.
    if(!finished){ waiting.remove();
      add('bot',render('The server stopped before answering. '+
        'The turn is still saved — run `python cli.py log` to see how far it got.'),
        'error'); }
  }catch(err){
    waiting.remove();
    add('bot',render('Lost the server: '+err.message+'. Is web.py still running?'),'error');
  }
  b.disabled=false; m.focus();
});
</script>
</body>
</html>
"""


def take_resume(name: str, data: bytes, conn) -> dict:
    """Take a resume PDF and make it the master. {"reply": ...} or {"error": ...}.

    Shared by the web page and the Telegram bot. Parsing it here rather than
    through a tool is deliberate: the file arrives as bytes, not as something
    the model chose, so there is nothing for it to get wrong. It only sees the
    result.
    """
    from resume import parse as parse_mod
    import vault as v

    name = Path(name).name
    if not name.lower().endswith(".pdf"):
        return {"error": f"{name} is not a PDF. Resumes are parsed from PDF "
                         f"so the text extracts the way an ATS reads it."}

    dest = paths.UPLOADS / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    try:
        master = parse_mod.parse(dest)
        rows = parse_mod.vault_rows(master)
        for k, val in rows.items():
            v.put(k, val, source="resume", conn=conn)
        # Where the file actually is. Both `apply` paths read this key and
        # nothing used to write it, so every application started with no
        # resume attached and the agent reported it could not find one.
        v.put("master_resume_path", str(dest), source="resume", conn=conn)
        rows["master_resume_path"] = str(dest)
    except Exception as exc:                            # noqa: BLE001
        traceback.print_exc()
        return {"error": f"Could not read {name}: {type(exc).__name__}: {exc}"}

    jobs = ", ".join(e.get("company", "?") for e in master.get("experience", []))
    # What the chat needs to carry on in its own words (Telegram). The fixed
    # reply below is the web page's.
    facts = {"name": master.get("name"), "roles": len(master.get("experience", [])),
             "companies": jobs, "saved": len(rows)}
    return {"facts": facts, "reply": (
        f"Read **{name}**.\n\n"
        f"I have you as **{master.get('name')}** — "
        f"{len(master.get('experience', []))} roles ({jobs}), "
        f"{len(rows)} details saved: {', '.join(rows)}.\n\n"
        f"Everything I put in a form comes from this, so tell me if any of "
        f"it is wrong. What kind of work are you looking for?")}


class Handler(BaseHTTPRequestHandler):
    # One conversation, one process. Set in main().
    session: dict = {}

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            return self._send(404, b"not found", "text/plain")
        s = self.session
        picks = s["state"]["picks"]
        page = (PAGE
                .replace("__PROVIDER__", f"{llm.provider()} / {llm.MODEL()}")
                .replace("__LOADED__",
                         f"{len(picks)} jobs loaded" if picks else "no search yet"))
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _upload(self, payload: dict) -> None:
        import base64

        out = take_resume(payload.get("name") or "resume.pdf",
                          base64.b64decode(payload["data"]), self.session["conn"])
        self._send(200, json.dumps(out).encode(), "application/json")

    def do_POST(self) -> None:
        if self.path not in ("/api/chat", "/api/upload"):
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:                                   # noqa: BLE001
            return self._send(400, b'{"error":"bad request"}', "application/json")

        if self.path == "/api/upload":
            return self._upload(payload)
        message = payload.get("message")
        if not message:
            return self._send(400, b'{"error":"bad request"}', "application/json")

        # Streamed, not a single blocking reply. A turn takes 15 seconds and
        # watching a spinner for 15 seconds tells you nothing about whether it
        # is working or stuck. One event per line, written as it happens.
        s = self.session
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def emit(ev: dict) -> None:
            self.wfile.write((json.dumps(ev) + "\n").encode())
            self.wfile.flush()

        try:
            text, s["history"] = chat.reply(
                message, s["history"], s["state"], s["conn"], on_step=emit)
            picks = s["state"]["picks"]
            emit({"kind": "reply", "text": text,
                  "loaded": f"{len(picks)} jobs loaded" if picks
                            else "no search yet"})
        except Exception as exc:                            # noqa: BLE001
            # Show the real error, and print it. Returning it only to the
            # browser loses it the moment the bubble scrolls away — which is
            # how one failed application became invisible.
            traceback.print_exc()
            try:
                emit({"kind": "error",
                      "text": f"{type(exc).__name__}: {exc}"})
            except Exception:                               # noqa: BLE001
                pass    # the client is gone; the traceback above is the record

    def log_message(self, *args) -> None:      # quiet; the agent's output is the log
        pass


def main(fixture: str | None = None, port: int = PORT, open_browser: bool = True) -> None:
    import chatlog

    conn = db.connect()
    # Background applications live in this process; any a previous run left
    # "running" were cut off by the restart, and must say so.
    from apply import worker
    worker.recover(conn)
    # History still dies with the process — but the transcript no longer does.
    session = chatlog.new_session()
    Handler.session = {
        "conn": conn,
        "history": [],
        "state": {"picks": chat.load_last(), "resumes": {}, "fixture": fixture,
                  "session": session},
    }
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"job agent · {llm.provider()}/{llm.MODEL()} · {url}")
    print(f"profile: {paths.label()}")
    print(f"logging to session {session} · `python cli.py log` to read it back")
    print("ctrl-c to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
