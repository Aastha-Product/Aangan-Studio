"""Web calls: callers talk to the agent from the browser (Vaani WebRTC), no phone number needed.

GET  /call                   public page with a "Start web call" button (LiveKit browser client)
POST /api/webcall/start      -> {room, url, token}   (Vaani trigger-call, medium=webrtc; limited per address and per day)
POST /api/webcall/end        the caller hung up: note the end time, then try to finish the call
GET  /api/webcall/status     the page asks until the call is processed (transcript -> five checks -> booking/alerts)

The transcript is pulled from Vaani's API after the call, so no Vaani webhook setting is needed. If the caller closes
the tab before that, sweep() finishes the call later (when the dashboard is opened, and from the 7pm job).
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import actions, config, vaani
from .calendly import parse_time
from .store import utcnow

PER_ADDRESS_PER_HOUR = 6
PER_DAY = 300                       # a ceiling on what the public page can spend in one day
ROOM_RE = re.compile(r"^webrtc-\d{6,}-[0-9a-f]{4,}$")


def valid_room(room: str) -> bool:
    return bool(room and ROOM_RE.match(room))


REF_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"        # no 0/o/1/l/i: easy for the agent to copy exactly


def new_call_ref() -> str:
    """A short one-time reference for one call (~1e11 combinations). Whoever holds it can use the booking tools
    for that call only, for a limited time, so the tools need no shared secret."""
    import secrets
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(8))


def start(store, ip: str, name: str = "") -> tuple[str, dict]:
    if not (config.VAANI_API_KEY and config.env("VAANI_AGENT_ID")):
        return "503 Service Unavailable", {"error": "Web calls aren't available right now. Please try again later."}
    key = f"webcall:{hashlib.sha256(ip.encode()).hexdigest()[:16]}"
    now = datetime.now(timezone.utc)
    if store.count_auth_failures(key, (now - timedelta(hours=1)).isoformat(timespec="seconds")) >= PER_ADDRESS_PER_HOUR:
        return "429 Too Many Requests", {"error": "You've started several calls in the last hour. Please try again a little later."}
    if store.event_stats("webcall_started", (now - timedelta(days=1)).isoformat(timespec="seconds"))[0] >= PER_DAY:
        return "429 Too Many Requests", {"error": "We're getting a lot of calls today. Please try again tomorrow."}
    name = re.sub(r"\s+", " ", name or "").strip()[:60]
    ref = new_call_ref()
    r = vaani.start_web_call(name or "Website caller", ref)
    room = r.get("room_name") or ""
    if not valid_room(room) or not r.get("token") or not r.get("connection_url"):
        return "502 Bad Gateway", {"error": "The call couldn't be started. Please try again."}
    store.record_auth_failure(key)          # counts towards the per-address limit
    store.upsert_call({"call_id": room, "channel": "web", "caller_name": name or None, "started_at": utcnow(),
                       "answered_at": utcnow(), "answered": True, "call_ref": ref})
    store.log_event(room, "webcall_started", {"from": key})
    return "200 OK", {"room": room, "url": r["connection_url"], "token": r["token"]}


def end(store, room: str, seconds) -> tuple[str, dict]:
    row = store.get_call(room) if valid_room(room) else None
    if not row or row.get("channel") != "web":
        return "404 Not Found", {"error": "no such call"}
    if not row.get("ended_at"):
        try:
            secs = max(0, min(int(float(seconds)), 3600))
        except (TypeError, ValueError):
            secs = None
        if secs is None and row.get("started_at"):
            secs = int((datetime.now(timezone.utc) - parse_time(row["started_at"])).total_seconds())
        store.update_call(room, {"ended_at": utcnow(), "duration_sec": secs})
        store.log_event(room, "webcall_ended", {"seconds": secs})
    return status(store, room)


def status(store, room: str) -> tuple[str, dict]:
    """Never tells the caller the outcome; only whether their details are saved."""
    row = store.get_call(room) if valid_room(room) else None
    if not row or row.get("channel") != "web":
        return "404 Not Found", {"error": "no such call"}
    return "200 OK", {"done": finish(store, row)}


def finish(store, row: dict) -> bool:
    """Fetch the transcript from Vaani and run the usual post-call steps, once. True when the call is processed."""
    if row.get("processed_at"):
        return True
    room = row["call_id"]
    transcript = vaani.get_transcript(room)
    if not transcript:
        return False                         # Vaani is still writing it; the page asks again
    if not store.claim_processing(room):
        return bool((store.get_call(room) or {}).get("processed_at"))
    summary = vaani.get_summary(room)
    if summary:
        store.update_call(room, {"summary": summary})
    started = parse_time(row["started_at"]) if row.get("started_at") else None
    actions.process_completed_call(store, room, vaani.normalise_transcript(transcript), started)
    return True


def sweep(store, limit: int = 3) -> int:
    """Finish web calls whose page was closed before the transcript was ready."""
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
    done = 0
    for row in store.list_calls(since):
        if done >= limit:
            break
        if row.get("channel") != "web" or row.get("processed_at") or not row.get("started_at"):
            continue
        if parse_time(row["started_at"]) > cutoff:
            continue                          # may still be in progress
        try:
            if finish(store, row):
                done += 1
        except Exception as e:  # noqa: BLE001 — a sweep must never break the page that triggered it
            store.log_event(row["call_id"], "error", {"where": "webcall sweep", "error": str(e)[:300]})
    return done


# --- the public page ------------------------------------------------------------------------------------

LIVEKIT_JS = "https://cdn.jsdelivr.net/npm/livekit-client@2.18.10/dist/livekit-client.umd.min.js"

PAGE_CSS = """
.callpage{min-height:100vh;display:grid;place-items:center;padding:24px 16px}
.callcard{width:100%;max-width:480px;padding:30px;text-align:center;overflow:hidden}
.callphoto{position:relative;margin:-30px -30px 22px;height:210px;background:#e9e3da center/cover no-repeat}
.callphoto::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 55%,rgba(0,0,0,.28))}
@media (max-width:520px){.callphoto{height:160px}}
.callcard .btn.primary{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}
.callcard .brand{justify-content:center;margin-bottom:22px}
.callcard h1{font-size:26px}
.callcard .lead{color:var(--text-secondary);font-size:15.5px;line-height:1.55;margin:10px 0 22px}
.callcard input{width:100%;border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);
border-radius:12px;padding:12px 14px;font:inherit;font-size:15px;text-align:center}
.bigbtn{width:100%;min-height:54px;font-size:16.5px;border-radius:14px;margin-top:12px;gap:10px}
.endbtn{background:var(--critical);color:#fff;border-color:var(--critical)}.endbtn:hover{opacity:.9;background:var(--critical)}
.fine{color:var(--muted);font-size:12.5px;line-height:1.5;margin-top:14px}
.orb{width:120px;height:120px;border-radius:50%;margin:8px auto 18px;background:var(--brand-soft);display:grid;place-items:center;
color:var(--brand);position:relative}
.orb::after{content:"";position:absolute;inset:-8px;border-radius:50%;border:2px solid var(--brand);opacity:0}
.orb.talking::after{animation:ring 1.2s ease-out infinite}
.orb.connecting{animation:breathe 1.6s ease-in-out infinite}
@keyframes ring{0%{transform:scale(.92);opacity:.7}100%{transform:scale(1.25);opacity:0}}
@keyframes breathe{0%,100%{transform:scale(1)}50%{transform:scale(.94)}}
.state{font-weight:600;font-size:16px}.timer{color:var(--muted);font-variant-numeric:tabular-nums;margin-top:4px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}
.row2 .btn{min-height:50px;border-radius:14px;font-size:15px}
.btn[aria-pressed="true"]{background:var(--warning-bg)}
.msgbox{border-radius:12px;padding:11px 14px;font-size:14px;margin-top:14px;text-align:left}
.msgbox.err{background:var(--critical-bg);color:var(--critical-text)}.msgbox.ok{background:var(--good-bg)}
@media (prefers-reduced-motion:reduce){.orb::after,.orb.connecting{animation:none}}
"""

PAGE_JS = """
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var room = null, roomId = null, started = 0, tick = null, ended = false;
  function show(view){ ['v-start','v-call','v-end'].forEach(function(v){ $(v).hidden = v !== view; }); }
  function setState(text, cls){ $('state').textContent = text; $('orb').className = 'orb ' + (cls || ''); }
  function err(text){ var b = $('err'); b.textContent = text; b.hidden = !text; }
  function clock(){ var s = Math.floor((Date.now() - started) / 1000); $('timer').textContent =
      String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }

  async function begin(){
    err(''); $('go').disabled = true; $('go').textContent = 'Starting…';
    try {
      if (!window.LivekitClient) throw new Error('lib');
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('mic');
      var mic = await navigator.mediaDevices.getUserMedia({audio: true});   // ask for the microphone first
      mic.getTracks().forEach(function(t){ t.stop(); });
      var r = await fetch('/api/webcall/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({name: $('name').value})});
      var d = await r.json();
      if (!r.ok) throw new Error('server:' + (d.error || 'The call could not be started.'));
      roomId = d.room;
      var LK = window.LivekitClient;
      room = new LK.Room({adaptiveStream: true});
      room.on(LK.RoomEvent.TrackSubscribed, function(track){
        if (track.kind === 'audio') { var el = track.attach(); el.autoplay = true; document.body.appendChild(el); }
      });
      room.on(LK.RoomEvent.ParticipantConnected, function(){ setState('Connected. Go ahead and speak.', ''); });
      room.on(LK.RoomEvent.ActiveSpeakersChanged, function(speakers){
        var agent = speakers.some(function(p){ return p.identity !== room.localParticipant.identity; });
        var me = speakers.some(function(p){ return p.identity === room.localParticipant.identity; });
        setState(agent ? 'The assistant is speaking…' : me ? 'Listening…' : 'Connected. Go ahead and speak.', agent ? 'talking' : '');
      });
      room.on(LK.RoomEvent.Disconnected, function(){ finish(); });
      show('v-call'); setState('Connecting you to the assistant…', 'connecting');
      await room.connect(d.url, d.token);
      await room.startAudio();
      await room.localParticipant.setMicrophoneEnabled(true);
      started = Date.now(); clock(); tick = setInterval(clock, 1000);
      if (room.remoteParticipants.size) setState('Connected. Go ahead and speak.', '');
    } catch (e) {
      var m = String(e && e.message || e);
      show('v-start'); $('go').disabled = false; $('go').innerHTML = $('go').dataset.label;
      if (m.indexOf('server:') === 0) err(m.slice(7));
      else if (m === 'lib') err('The call could not load. Check your internet connection and try again.');
      else if (/Permission|NotAllowed|mic|denied/i.test(m)) err('We couldn\\'t use your microphone. Allow microphone access for this page in your browser, then try again.');
      else err('The call could not be connected. Please try again.');
      if (room) { try { room.disconnect(); } catch (x) {} }
    }
  }

  async function finish(){
    if (ended) return; ended = true;
    clearInterval(tick);
    var secs = started ? Math.round((Date.now() - started) / 1000) : 0;
    if (room) { try { room.disconnect(); } catch (x) {} }
    show('v-end');
    if (!roomId) return;
    var body = JSON.stringify({room: roomId, seconds: secs});
    try { await fetch('/api/webcall/end', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: body, keepalive: true}); } catch (x) {}
    // keep asking until the details are saved (the transcript takes a little while to be ready)
    for (var i = 0; i < 40; i++) {
      await new Promise(function(r){ setTimeout(r, 5000); });
      try {
        var r = await fetch('/api/webcall/status?room=' + encodeURIComponent(roomId));
        var d = await r.json();
        if (d.done) { $('saving').textContent = 'Your details are with our team.'; return; }
      } catch (x) {}
    }
  }

  $('go').dataset.label = $('go').innerHTML;
  $('go').addEventListener('click', begin);
  $('hang').addEventListener('click', finish);
  $('mute').addEventListener('click', function(){
    if (!room) return;
    var on = $('mute').getAttribute('aria-pressed') !== 'true';
    room.localParticipant.setMicrophoneEnabled(!on);
    $('mute').setAttribute('aria-pressed', on ? 'true' : 'false');
    $('mute').textContent = on ? 'Unmute' : 'Mute';
  });
  window.addEventListener('pagehide', function(){
    if (roomId && !ended) navigator.sendBeacon('/api/webcall/end', new Blob([JSON.stringify({room: roomId})], {type: 'application/json'}));
  });
})();
"""


def render_page() -> str:
    from .dashboard import CSS, CSS_V2, FAVICON, FONT_LINK, _icon, photo_credit, photo_url
    phone = _icon("phone", 20)
    photo = (f"<div class=\"callphoto\" style=\"background-image:url('{photo_url('call', 960)}')\" role=\"img\" "
             f"aria-label=\"A bright, sunlit living room with wooden furniture and plants\">{photo_credit('call')}</div>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{FONT_LINK}<link rel="icon" href="{FAVICON}"><title>Talk to Aangan Studio</title>
<meta name="description" content="Talk to Aangan Studio's assistant about designing your home or office in Pune, any time.">
<style>{CSS}{CSS_V2}{PAGE_CSS}</style></head>
<body class="viz-root"><main class="callpage"><div class="card callcard">
{photo}
<div class="brand"><span class="mark">A</span><span style="text-align:left"><b>Aangan Studio</b><small>Interior design · Pune</small></span></div>

<section id="v-start">
<h1>Talk to Aangan Studio</h1>
<p class="lead">Tell us about your home or office project. Our assistant answers any time, day or night, and can set up
a consultation with one of our designers.</p>
<label class="sr" for="name">Your name (optional)</label>
<input id="name" type="text" autocomplete="name" placeholder="Your name (optional)" maxlength="60">
<button id="go" class="btn primary bigbtn" type="button">{phone}Start web call</button>
<div id="err" class="msgbox err" role="alert" hidden></div>
<p class="fine">Uses your microphone; no phone number needed. The call is recorded and transcribed so our designers
have your details before they speak to you.</p>
</section>

<section id="v-call" hidden>
<div id="orb" class="orb connecting" aria-hidden="true">{_icon("phone", 34)}</div>
<div id="state" class="state" role="status" aria-live="polite">Connecting…</div>
<div id="timer" class="timer">00:00</div>
<div class="row2"><button id="mute" class="btn" type="button" aria-pressed="false">Mute</button>
<button id="hang" class="btn endbtn" type="button">End call</button></div>
<p class="fine">Speak normally. You can interrupt the assistant at any time.</p>
</section>

<section id="v-end" hidden>
<h1>Thank you for calling</h1>
<p class="lead">If we booked a consultation, a confirmation email with the calendar invite is on its way. If our team
needs anything else, we'll get in touch.</p>
<p id="saving" class="fine" role="status">Saving your details…</p>
<a class="btn bigbtn" href="/call">Start another call</a>
</section>

</div></main>
<script src="{LIVEKIT_JS}" crossorigin="anonymous"></script>
<script>{PAGE_JS}</script></body></html>"""
