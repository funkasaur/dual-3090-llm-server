#!/usr/bin/env python3
"""qwenq — a small, bounded queue for background Qwen work on llama-swap.

Background jobs (nightly digests, shadow forecasts, document work) go through here instead of calling
llama-swap directly, so they share one set of rules:

  * one request in flight, run by a single worker (`qwenq worker`, systemd user service qwen-queue);
  * a job starts only when the main model is already loaded and its slot is idle — background work never
    loads another model (that would evict the main Qwen and its prompt cache) and yields to live callers;
  * an hourly occupancy budget (seconds of generation per rolling hour) caps how much GPU time it takes;
  * per-job priority (lower runs first), deadline, output cap, bounded retries with exponential backoff;
  * responses are streamed, so a cancel or a passed deadline closes the connection and llama-server stops;
  * an answer with no final content (a reasoning-only stop) is a failure, not a success;
  * every job, its timings and its result persist in SQLite (queue wait, run time, tokens).

Live callers (agent jobs, Open WebUI, the Telegram bot) still call llama-swap directly and are not
queued; the idle gate is what keeps background work out of their way. Limits of that, by design:
  * the gate is advisory — a live request or a model swap can arrive between the check and the POST;
  * if the router is EMPTY, a job for the main model loads it (what a live caller would do anyway);
  * the budget is checked when a job starts, so one job can run past the hour's remainder; background jobs
    (priority >= 6) get only 75% of it, the rest is kept for higher-priority work;
  * a job that has started cannot be preempted by priority, so keep max_tokens modest.

Library:  import qwenq; jid = qwenq.submit(messages, kind="digest"); job = qwenq.wait(jid, timeout=3600)
CLI:      qwenq submit --kind test --prompt "Say hi" --wait
          qwenq list | get <id> | cancel <id> | stats | worker
"""
import argparse
import http.client
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SWAP = os.environ.get("QWENQ_SWAP", "http://127.0.0.1:3060")
MAIN_MODEL = os.environ.get("QWENQ_MODEL", "Qwen3.8-27B-Q8-Medium-Reason")
DB_PATH = os.path.expanduser(os.environ.get("QWENQ_DB", "~/.local/share/qwen-queue/queue.db"))
BUDGET_S = int(os.environ.get("QWENQ_BUDGET_S", "1200"))      # generation seconds per rolling hour
MAX_TOKENS_CAP = int(os.environ.get("QWENQ_MAX_TOKENS_CAP", "16384"))
IDLE_CHECKS = int(os.environ.get("QWENQ_IDLE_CHECKS", "2"))     # consecutive idle samples before starting
IDLE_GAP_S = float(os.environ.get("QWENQ_IDLE_GAP_S", "3"))
POLL_S = float(os.environ.get("QWENQ_POLL_S", "10"))
# Background-priority jobs (priority >= LOW_PRIORITY) may use only this share of the hourly budget, so bulk work
# (shadow ensembles) can never starve a higher-priority job (the nightly narration) of the whole hour.
LOW_PRIORITY = int(os.environ.get("QWENQ_LOW_PRIORITY", "6"))
LOW_SHARE = float(os.environ.get("QWENQ_LOW_SHARE", "0.75"))
READ_TIMEOUT_S = int(os.environ.get("QWENQ_READ_TIMEOUT_S", "300"))  # max silence between stream chunks
TERMINAL = ("done", "failed", "cancelled", "expired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5,
  status TEXT NOT NULL DEFAULT 'pending',
  request TEXT NOT NULL,
  dedupe_key TEXT,
  created REAL NOT NULL,
  deadline REAL NOT NULL,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try REAL NOT NULL DEFAULT 0,
  started REAL,
  finished REAL,
  run_s REAL NOT NULL DEFAULT 0,
  content TEXT,
  reasoning TEXT,
  usage TEXT,
  error TEXT,
  cancel INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, priority, created);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS runs (          -- one row per attempt; feeds the hourly budget and stats
  job_id TEXT NOT NULL, attempt INTEGER NOT NULL, started REAL NOT NULL, finished REAL, outcome TEXT
);
CREATE INDEX IF NOT EXISTS runs_time ON runs(started);
"""


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


# ── client API ──────────────────────────────────────────────────────────────────────────────────────
def submit(messages, kind="adhoc", priority=5, deadline_s=3600, max_tokens=4096, max_attempts=3,
           dedupe_key=None, **params):
    """Queue a chat request. `params` go into the request body (temperature, chat_template_kwargs, ...).
    With dedupe_key, an existing job with the same key is returned instead of queuing a duplicate."""
    body = dict(params)
    body["messages"] = messages
    body["max_tokens"] = min(int(max_tokens), MAX_TOKENS_CAP)
    body.setdefault("model", MAIN_MODEL)
    now = time.time()
    jid = uuid.uuid4().hex[:12]
    c = db()
    if dedupe_key:
        row = c.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if row:
            return row["id"]
    c.execute("INSERT INTO jobs(id,kind,priority,request,dedupe_key,created,deadline,max_attempts) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (jid, kind, int(priority), json.dumps(body), dedupe_key, now, now + deadline_s, int(max_attempts)))
    return jid


def get(jid):
    row = db().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(row) if row else None


def wait(jid, timeout=3600, poll=5):
    """Block until the job is terminal or `timeout` passes; returns the job dict either way."""
    end = time.time() + timeout
    while True:
        job = get(jid)
        if job is None or job["status"] in TERMINAL or time.time() >= end:
            return job
        time.sleep(poll)


def cancel(jid):
    c = db()
    c.execute("UPDATE jobs SET status='cancelled', finished=?, error='cancelled before start' "
              "WHERE id=? AND status='pending'", (time.time(), jid))
    c.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status='running'", (jid,))


# ── worker ──────────────────────────────────────────────────────────────────────────────────────────
def _http_json(path, timeout=10):
    with urllib.request.urlopen(SWAP + path, timeout=timeout) as r:
        return json.load(r)


def gate(model):
    """Return None if `model` may run now, else a short reason to wait."""
    try:
        running = _http_json("/running").get("running", [])
    except Exception as e:
        return f"llama-swap unreachable ({type(e).__name__})"
    loaded = {x.get("model"): x.get("state") for x in running}
    if model not in loaded:
        # Never trigger a swap: only the main model may be loaded on demand, and only onto an empty router.
        if loaded or model != MAIN_MODEL:
            return f"{model} not loaded (loaded: {', '.join(loaded) or 'none'})"
        return None
    if loaded[model] != "ready":
        return f"{model} is {loaded[model]}"
    for i in range(IDLE_CHECKS):
        try:
            slots = _http_json(f"/upstream/{model}/slots")
        except Exception as e:
            return f"slots unreadable ({type(e).__name__})"
        if any(s.get("is_processing") for s in slots):
            return "model busy"
        if i + 1 < IDLE_CHECKS:
            time.sleep(IDLE_GAP_S)
    return None


def budget_used(c, now):
    """Seconds of generation in the last hour, counting the overlap of each attempt with the window."""
    lo = now - 3600
    used = 0.0
    for r in c.execute("SELECT started, finished FROM runs WHERE COALESCE(finished, ?) > ?", (now, lo)):
        used += (r["finished"] or now) - max(r["started"], lo)
    return used


def run_request(c, job, body):
    """Stream one chat completion. Returns (content, reasoning, usage). Raises on error/cancel/deadline."""
    body = dict(body, stream=True, stream_options={"include_usage": True})
    url = urllib.parse.urlsplit(SWAP)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=READ_TIMEOUT_S)
    content, reasoning, usage, timings = [], [], None, None
    stopped = {}
    done = threading.Event()

    def watch():
        # Checks cancel/deadline every 2 s even while the stream is silent (a long prefill sends nothing);
        # shutting the socket down unblocks the read, and llama-server stops generating when it sees it.
        wc = db()
        while not done.wait(2):
            row = wc.execute("SELECT cancel, deadline FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row["cancel"] or time.time() > row["deadline"]:
                stopped["why"] = "cancel" if row["cancel"] else "deadline"
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    try:
        conn.connect()
        watcher.start()
        conn.request("POST", (url.path.rstrip("/") or "") + "/v1/chat/completions", body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {r.read(500).decode('utf-8', 'replace')}")
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("error"):
                raise RuntimeError(f"server error: {ev['error']}")
            usage = ev.get("usage") or usage
            timings = ev.get("timings") or timings
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
    except (OSError, http.client.HTTPException, ValueError, RuntimeError):
        if not stopped:
            raise
    finally:
        done.set()
        conn.close()
    if stopped.get("why") == "cancel":
        raise InterruptedError("cancelled while running")
    if stopped.get("why") == "deadline":
        raise TimeoutError("deadline passed while running")
    u = dict(usage or {})
    if timings:
        u["timings"] = timings
    return "".join(content), "".join(reasoning), u


def work_one(c, log):
    now = time.time()
    c.execute("UPDATE jobs SET status='expired', finished=?, error=COALESCE(error,'deadline passed before start') "
              "WHERE status='pending' AND deadline < ?", (now, now))
    jobs = c.execute("SELECT * FROM jobs WHERE status='pending' AND next_try <= ? "
                     "ORDER BY priority, created", (now,)).fetchall()
    if not jobs:
        return False
    used = budget_used(c, now)
    low_cap = BUDGET_S * LOW_SHARE
    if used >= BUDGET_S or all(j["priority"] >= LOW_PRIORITY for j in jobs) and used >= low_cap:
        log(f"budget: {used:.0f}/{BUDGET_S}s used this hour (background share {low_cap:.0f}s); waiting")
        return False
    # Gate once per model, in queue order, so a job waiting for an unloaded model doesn't block the rest.
    job, verdicts = None, {}
    for cand in jobs:
        if cand["priority"] >= LOW_PRIORITY and used >= low_cap:
            continue
        model = json.loads(cand["request"]).get("model", MAIN_MODEL)
        if model not in verdicts:
            verdicts[model] = gate(model)
        if verdicts[model] is None:
            job = cand
            break
    if job is None:
        log("waiting: " + "; ".join(f"{m}: {w}" for m, w in verdicts.items()))
        return False
    body = json.loads(job["request"])
    attempt = job["attempts"] + 1
    t0 = time.time()
    c.execute("UPDATE jobs SET status='running', attempts=?, started=COALESCE(started, ?) WHERE id=?",
              (attempt, t0, job["id"]))
    c.execute("INSERT INTO runs(job_id, attempt, started) VALUES(?,?,?)", (job["id"], attempt, t0))
    log(f"start {job['id']} ({job['kind']}, p{job['priority']}, attempt {attempt}/{job['max_attempts']}, "
        f"waited {t0 - job['created']:.0f}s)")
    outcome, err = "done", None
    try:
        content, reasoning, usage = run_request(c, job, body)
        if not content.strip():
            raise ValueError(f"reasoning-only stop: no final content ({len(reasoning)} reasoning chars)")
    except InterruptedError as e:
        outcome, err = "cancelled", str(e)
    except TimeoutError as e:
        outcome, err = "expired", str(e)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, RuntimeError) as e:
        outcome, err = "retry", f"{type(e).__name__}: {e}"
    t1 = time.time()
    c.execute("UPDATE runs SET finished=?, outcome=? WHERE job_id=? AND attempt=?", (t1, outcome, job["id"], attempt))
    run_s = job["run_s"] + (t1 - t0)
    if outcome == "done":
        c.execute("UPDATE jobs SET status='done', finished=?, run_s=?, content=?, reasoning=?, usage=?, error=NULL "
                  "WHERE id=?", (t1, run_s, content, reasoning, json.dumps(usage), job["id"]))
    elif outcome == "retry" and attempt < job["max_attempts"]:
        backoff = 60 * 2 ** (attempt - 1)
        c.execute("UPDATE jobs SET status='pending', next_try=?, run_s=?, error=? WHERE id=?",
                  (t1 + backoff, run_s, err, job["id"]))
        outcome = f"retry in {backoff}s"
    else:
        final = "failed" if outcome == "retry" else outcome
        c.execute("UPDATE jobs SET status=?, finished=?, run_s=?, error=? WHERE id=?",
                  (final, t1, run_s, err, job["id"]))
    log(f"end {job['id']}: {outcome} in {t1 - t0:.0f}s" + (f" — {err}" if err else ""))
    return True


def worker():
    def log(msg, _last=[None]):
        if msg != _last[0]:                  # don't repeat the same waiting line every poll
            print(time.strftime("%F %T"), msg, flush=True)
            _last[0] = msg
    c = db()
    # A job left 'running' by a crash or restart goes back to the queue (counts as a used attempt).
    n = c.execute("UPDATE jobs SET status='pending', error='worker restarted mid-run' WHERE status='running'").rowcount
    c.execute("UPDATE runs SET finished=started, outcome='worker restart' WHERE finished IS NULL")
    log(f"worker up: db={DB_PATH} model={MAIN_MODEL} budget={BUDGET_S}s/h requeued={n}")
    while True:
        try:
            if not work_one(c, log):
                time.sleep(POLL_S)
        except Exception as e:                # keep the worker alive; the job row keeps its state
            log(f"worker error: {type(e).__name__}: {e}")
            time.sleep(POLL_S)


# ── stats / CLI ─────────────────────────────────────────────────────────────────────────────────────
def stats(hours=24):
    c = db()
    since = time.time() - hours * 3600
    by = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM jobs WHERE created >= ? OR status IN ('pending','running') GROUP BY status",
        (since,))}
    waits = sorted(r["w"] for r in c.execute(
        "SELECT started - created w FROM jobs WHERE started IS NOT NULL AND created >= ?", (since,)))
    runs = sorted(r["run_s"] for r in c.execute(
        "SELECT run_s FROM jobs WHERE status='done' AND created >= ?", (since,)))
    oldest = c.execute("SELECT MIN(created) m FROM jobs WHERE status='pending'").fetchone()["m"]

    def pct(xs, p):
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else None
    return {"hours": hours, "by_status": by,
            "queue_wait_s": {"p50": pct(waits, .5), "p95": pct(waits, .95)},
            "run_s": {"p50": pct(runs, .5), "p95": pct(runs, .95), "total": round(sum(runs), 1)},
            "budget_used_s_last_hour": round(budget_used(c, time.time()), 1), "budget_s": BUDGET_S,
            "oldest_pending_age_s": round(time.time() - oldest, 1) if oldest else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="queue a job (request JSON on stdin/--file, or --prompt)")
    s.add_argument("--kind", default="adhoc")
    s.add_argument("--priority", type=int, default=5)
    s.add_argument("--deadline-min", type=float, default=60)
    s.add_argument("--max-tokens", type=int, default=4096)
    s.add_argument("--attempts", type=int, default=3)
    s.add_argument("--dedupe")
    s.add_argument("--prompt")
    s.add_argument("--system")
    s.add_argument("--file")
    s.add_argument("--wait", action="store_true")
    for name in ("get", "cancel"):
        sub.add_parser(name).add_argument("id")
    lp = sub.add_parser("list")
    lp.add_argument("-n", type=int, default=20)
    sp = sub.add_parser("stats")
    sp.add_argument("--hours", type=float, default=24)
    sub.add_parser("worker")
    a = ap.parse_args()

    if a.cmd == "worker":
        worker()
    elif a.cmd == "submit":
        if a.prompt:
            msgs = ([{"role": "system", "content": a.system}] if a.system else []) + \
                   [{"role": "user", "content": a.prompt}]
            extra = {}
        else:
            body = json.load(open(a.file) if a.file else sys.stdin)
            msgs = body.pop("messages")
            body.pop("max_tokens", None)
            extra = body
        jid = submit(msgs, kind=a.kind, priority=a.priority, deadline_s=a.deadline_min * 60,
                     max_tokens=a.max_tokens, max_attempts=a.attempts, dedupe_key=a.dedupe, **extra)
        if not a.wait:
            print(jid)
            return
        job = wait(jid, timeout=a.deadline_min * 60 + 60)
        print(json.dumps({k: job[k] for k in ("id", "status", "error", "run_s", "usage")}, indent=1))
        if job["status"] == "done":
            print("\n" + job["content"])
        sys.exit(0 if job["status"] == "done" else 1)
    elif a.cmd == "get":
        job = get(a.id)
        if not job:
            sys.exit(f"no job {a.id}")
        job.pop("reasoning", None)
        print(json.dumps(job, indent=1))
    elif a.cmd == "cancel":
        cancel(a.id)
        print(json.dumps({k: get(a.id)[k] for k in ("id", "status", "cancel")}))
    elif a.cmd == "list":
        for r in db().execute("SELECT id, kind, priority, status, attempts, created, run_s, error FROM jobs "
                              "ORDER BY created DESC LIMIT ?", (a.n,)):
            print(f"{r['id']}  {time.strftime('%m-%d %H:%M', time.localtime(r['created']))}  {r['kind']:<16} "
                  f"p{r['priority']} {r['status']:<9} a{r['attempts']} {r['run_s']:6.0f}s  {r['error'] or ''}")
    elif a.cmd == "stats":
        print(json.dumps(stats(a.hours), indent=1))


if __name__ == "__main__":
    main()
