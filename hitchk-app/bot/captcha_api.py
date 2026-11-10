"""The captcha solver, on this machine, as an api.

The captchas are answered here, in a browser on this machine: nothing is
sent away and nothing is paid for, and no service in between is
needed.  The solver is the one the gates already use, in
local_hcaptcha_solver.py, so the answer here and the answer in the
gates are the same answer.

The api has a key of its own, made here, and kept here.  No account
anywhere else is needed.

The api speaks the legacy 2captcha protocol, so a client written for
that one works with this one:

  GET|POST /in.php
      key, method, google_sitekey (or googlekey, or sitekey), pageurl,
      proxy, action, data, json=1
      ->  {"status": 1, "request": "<job id>"}          (the work is taken)
          {"status": 0, "request": "ERROR_WRONG_USER_KEY"}
          {"status": 0, "request": "ERROR_METHOD_NOT_SUPPORTED"}
          {"status": 0, "request": "ERROR_NO_SLOT_AVAILABLE"}
      without json=1 the answer is the plain text "OK|<job id>"

  GET|POST /res.php
      key, action=get, id=<job id>, json=1
      ->  {"status": 1, "request": "<the token>"}        (answered)
          {"status": 0, "request": "CAPCHA_NOT_READY"}   (still working)
          {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}
          {"status": 0, "request": "ERROR_WRONG_CAPTCHA_ID"}
          {"status": 0, "request": "ERROR_WRONG_USER_KEY"}
      without json=1 the answer is the plain text "OK|<the token>",
      "CAPCHA_NOT_READY", or the ERROR_* code.

The status is never 2: the token and the error codes are both carried
in "request", which is what the legacy clients read.

There are the same things in a plainer shape as well:

  GET  /health              is it up, and what can it do
  POST /solve               an hCaptcha
  POST /solve/recaptcha     a reCAPTCHA
  POST /solve/turnstile     a Cloudflare Turnstile
  GET  /stats               how many were asked for, and how many were answered
"""

import asyncio
import os
import sys
import time
import secrets
import logging
import threading
import uuid
import queue
from contextlib import asynccontextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("captcha_api")

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

APP_NAME = "captcha-api"
APP_VERSION = "4.0"
KEY_FILE = os.path.join(_HERE, "captcha_api_key.txt")

# The codes of the legacy protocol.
ERR_WRONG_USER_KEY = "ERROR_WRONG_USER_KEY"
ERR_WRONG_CAPTCHA_ID = "ERROR_WRONG_CAPTCHA_ID"
ERR_CAPTCHA_UNSOLVABLE = "ERROR_CAPTCHA_UNSOLVABLE"
ERR_METHOD_NOT_SUPPORTED = "ERROR_METHOD_NOT_SUPPORTED"
ERR_NO_SLOT_AVAILABLE = "ERROR_NO_SLOT_AVAILABLE"
ERR_BAD_PARAMETERS = "ERROR_BAD_PARAMETERS"
NOT_READY = "CAPCHA_NOT_READY"

# The work can be asked for and left for a person to answer: some
# captchas are a picture and need eyes.  The job then waits, and the
# person hands the token in.
MANUAL = "manual"

# The methods that are known, and the kind of captcha each one means.
# A method that is not here is turned away, and not taken for the
# hCaptcha it is not.
METHODS = {
    "hcaptcha": "hcaptcha",
    "recaptcha": "recaptcha",
    "userrecaptcha": "recaptcha",
    "recaptchav2": "recaptcha",
    "recaptchav3": "recaptcha",
    "turnstile": "turnstile",
}

# How long a job has, how many work at once, how many may wait, and
# how long an answer is kept.
JOB_DEADLINE = float(os.environ.get("CAPTCHA_JOB_DEADLINE", "240"))

# What the solver keeps for itself: the browser is given a while
# longer than the time it is given, so the job's limit has to be
# longer than the solver's.
SOLVER_OVERHEAD = float(os.environ.get("CAPTCHA_SOLVER_OVERHEAD", "100"))
MAX_CONCURRENT = max(1, int(os.environ.get("CAPTCHA_MAX_CONCURRENT", "4")))
MAX_QUEUED = max(1, int(os.environ.get("CAPTCHA_MAX_QUEUED", "24")))
RESULT_TTL = float(os.environ.get("CAPTCHA_RESULT_TTL", "300"))

# The counters, so the owner can see what the api has been asked for.
COUNTS = {"asked": 0, "answered": 0, "failed": 0, "refused": 0, "by_kind": {}}
STARTED = time.time()

# The jobs, and the queue of the work that is waiting.
JOBS = {}
JOBS_LOCK = threading.Lock()
WORK_QUEUE = queue.Queue(maxsize=MAX_QUEUED)
WORKERS = []


def _the_key() -> str:
    """The key of the api.

    It is made here, once, and kept in the file next to this one.  It
    is not made again when the api is started again: a key that changes
    on every restart is no key at all.  When it is in the environment
    it is used from there, so the owner can put it wherever they like.
    """
    key = (os.environ.get("CAPTCHA_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except Exception:
        pass
    key = secrets.token_urlsafe(32)
    try:
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    logger.warning("A NEW KEY HAS BEEN MADE FOR THE CAPTCHA API, and it is "
                   "written down in %s", KEY_FILE)
    return key


API_KEY = _the_key()


def _hide_the_proxy(words: str) -> str:
    """The address of the proxy is taken out of the words.

    The address has a name and a password in it, and it comes in the
    address of the request, so it would be written down.
    """
    out = []
    rest = words
    while True:
        at = rest.find("proxy=")
        if at < 0:
            out.append(rest)
            break
        out.append(rest[:at])
        rest = rest[at + len("proxy="):]
        end = rest.find("&")
        if end < 0:
            out.append("the proxy")
            break
        out.append("the proxy")
        rest = rest[end:]
    return "".join(out)


class _HideTheSecrets(logging.Formatter):
    """The key and the addresses are not written in the log.

    The key comes in the address, the way the legacy clients send it,
    and the access log writes the whole address.  The words are made
    first and the secrets are taken out of them afterwards: taking them
    out of the record itself would break the making of them.
    """

    def format(self, record):
        try:
            words = super().format(record)
        except Exception:
            return ""
        if API_KEY:
            words = words.replace(API_KEY, "the key")
        words = _hide_the_proxy(words)
        return words


def _hide_the_secrets_in_the_log():
    """Every place the log is written has the secrets taken out of it."""
    for name in ("uvicorn.access", "uvicorn.error", "captcha_api", ""):
        the_logger = logging.getLogger(name) if name else logging.getLogger()
        for handler in list(the_logger.handlers):
            old = handler.formatter
            fmt = getattr(old, "_fmt", None) or "%(message)s"
            datefmt = getattr(old, "datefmt", None)
            handler.setFormatter(_HideTheSecrets(fmt, datefmt))
        if not the_logger.handlers and name:
            handler = logging.StreamHandler()
            handler.setFormatter(_HideTheSecrets("%(message)s"))
            the_logger.addHandler(handler)


def _the_key_is_right(given: str) -> bool:
    return bool(given) and secrets.compare_digest(str(given), API_KEY)


def _count(kind: str, answered: bool):
    COUNTS["asked"] += 1
    COUNTS["by_kind"][kind] = COUNTS["by_kind"].get(kind, 0) + 1
    if answered:
        COUNTS["answered"] += 1
    else:
        COUNTS["failed"] += 1



def _the_browser(words):
    """The name of the browser, out of the words that came with the
    work.  Nothing comes back when no name came with the work."""
    if not words:
        return None
    try:
        import json
        said = json.loads(words) if isinstance(words, str) else words
    except Exception:
        return None
    if not isinstance(said, dict):
        return None
    try:
        from browser_identity import Fingerprint
        hints = said.get("hints") or {}
        platform = hints.get("platform") or "Linux"
        system = {"Windows": "Windows NT 10.0; Win64; x64",
                  "macOS": "Macintosh; Intel Mac OS X 10_15_7"}.get(
                      platform, "X11; Linux x86_64")
        language = said.get("language") or "en-US,en;q=0.9"
        screen = tuple(said.get("screen") or (1920, 1080, 24))
        return Fingerprint(system, platform, language, screen,
                           ua=said.get("user_agent"))
    except Exception:
        return None

async def _the_token(kind: str, sitekey: str, url: str, rqdata: str = "",
                     proxy: str = "", timeout: int = 0, action: str = "",
                     cdata: str = "", browser: str = ""):
    """The captcha is answered, here, in a browser.

    The token comes from the solver and from nowhere else: no token is
    made up here, and a job that was taken is not a job that was
    answered.
    """
    t0 = time.time()
    token = None
    reason = ""
    # The solver is given the time the job has, less what the solver
    # keeps for itself: the solver gives its browser a while longer
    # than the time it is given, so the job's limit has to be longer
    # than the solver's.  Otherwise the api walks away from a solve
    # that is still running, and the worker stays stuck with the
    # browser in its hands and no later card is answered at all.
    the_time = max(20, (timeout or int(JOB_DEADLINE)) - SOLVER_OVERHEAD)
    try:
        if kind == "hcaptcha":
            from local_hcaptcha_solver import solve_hcaptcha_local
            token = await solve_hcaptcha_local(sitekey, url, rqdata=rqdata or None,
                                               proxy=proxy or None,
                                               timeout=the_time,
                                               fingerprint=_the_browser(browser))
        elif kind == "recaptcha":
            from local_hcaptcha_solver import solve_recaptcha_v3_local
            token = await solve_recaptcha_v3_local(sitekey, url, proxy=proxy or None,
                                                   timeout=the_time,
                                                   fingerprint=_the_browser(browser))
        elif kind == "turnstile":
            from local_hcaptcha_solver import solve_turnstile_local
            token = await solve_turnstile_local(sitekey, url, proxy=proxy or None,
                                                timeout=the_time,
                                                action=action or None,
                                                cdata=cdata or None,
                                                fingerprint=_the_browser(browser))
        else:
            reason = "the captcha kind is not known: %s" % kind
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, str(e)[:120])
        logger.warning("the %s could not be answered: %s", kind, reason)

    took = round(time.time() - t0, 2)
    if token:
        _count(kind, True)
        logger.info("the %s was answered in %ss", kind, took)
        return {"status": "ok", "kind": kind, "token": token, "elapsed": took}
    _count(kind, False)
    logger.info("the %s was not answered (%ss)", kind, took)
    return {"status": "error", "kind": kind, "token": None,
            "reason": reason or "the captcha was not answered", "elapsed": took}


# ── the work
def _put_job(job_id: str, job: dict):
    with JOBS_LOCK:
        JOBS[job_id] = job


def _get_job(job_id: str):
    with JOBS_LOCK:
        return JOBS.get(job_id)


def _finish_job(job_id: str, answer: dict):
    """The job is marked done and its answer put away, both at once.

    A reader that comes at the moment between the two would find a job
    that is done and has no answer, and would say the captcha could not
    be answered when it was answered.  So both are done under the lock.

    A job that has run out of time keeps the answer it was given: a
    late result does not write over a job that has already been given
    up on.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        if job.get("state") in ("expired", "given up"):
            job["late"] = True
            return
        job["answer"] = answer
        job["done"] = True
        job["state"] = "done"
        job["finished_at"] = time.time()


def _give_up_job(job_id: str, why: str):
    """The job is given up on, and a late result may not write over it."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job["state"] = "given up"
        job["done"] = True
        job["answer"] = {"status": "error", "token": None,
                         "reason": why, "elapsed": 0}
        job["given_up_at"] = time.time()


def _expire_job(job_id: str, why: str):
    """The job is thrown away, and a late result may not write over it."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job["state"] = "expired"
        job["done"] = True
        job["answer"] = {"status": "error", "token": None,
                         "reason": why, "elapsed": 0}
        job["expired_at"] = time.time()


def _the_workers():
    """The workers that do the work, one job at a time each."""
    for _ in range(MAX_CONCURRENT):
        thread = threading.Thread(target=_the_worker, name="captcha-worker",
                                  daemon=True)
        thread.start()
        WORKERS.append(thread)


# The browser work is done in one loop of its own.
#
# The solver keeps a browser and a page between the cards, and a
# browser belongs to the loop it was made in.  A new loop for every
# card took the browser away from the card that came after it, and the
# worker gave up with a word about a task belonging to another loop.
# So there is one loop, and every card is done in it.
_LOOP = None
_LOOP_THREAD = None
_LOOP_READY = threading.Event()


def _the_loop():
    """The one loop the browser work is done in."""
    global _LOOP
    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    _LOOP_READY.set()
    try:
        _LOOP.run_forever()
    except Exception:
        pass


def _the_loop_ready():
    global _LOOP_THREAD
    if _LOOP_THREAD is None:
        _LOOP_THREAD = threading.Thread(target=_the_loop,
                                        name="captcha-loop", daemon=True)
        _LOOP_THREAD.start()
    _LOOP_READY.wait(15)


def _run_in_the_loop(coro, timeout=0):
    """Do the work in that loop, and wait for the answer."""
    _the_loop_ready()
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _LOOP)
        return future.result(timeout=timeout if timeout else None)
    except asyncio.TimeoutError:
        logger.warning("the browser work took too long and was given up on")
        return {"ok": False, "error": "the work took too long"}


def _the_worker():
    """One worker: it takes a job and does it."""
    while True:
        job_id = WORK_QUEUE.get()
        try:
            job = _get_job(job_id)
            if job is None or job.get("state") != "queued":
                continue
            if time.time() > job.get("deadline", 0):
                _give_up_job(job_id, "the work ran out of time before it started")
                continue
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is not None:
                    job["state"] = "working"
                    job["started_at"] = time.time()
            answer = _run_in_the_loop(_the_token(
                job["kind"], job["sitekey"], job["url"], job["rqdata"],
                job["proxy"], job["timeout"], job["action"], job["cdata"],
            job.get("browser") or ""),
                timeout=(job.get("timeout") or 120) + 150)
            _finish_job(job_id, answer)
        except Exception as e:
            logger.warning("a job could not be done: %s: %s",
                           type(e).__name__, str(e)[:90])
            _give_up_job(job_id, "%s: %s" % (type(e).__name__, str(e)[:90]))
        finally:
            try:
                WORK_QUEUE.task_done()
            except Exception:
                pass


def _the_janitor_once():
    """The old answers are thrown away, so the api does not fill up."""
    now = time.time()
    with JOBS_LOCK:
        for job_id in [k for k, v in JOBS.items()
                       if v.get("asked_at", now) < now - RESULT_TTL]:
            JOBS.pop(job_id, None)


def _the_janitor():
    """The old answers are thrown away, now and then."""
    while True:
        time.sleep(30)
        _the_janitor_once()


def _the_answer(ok: bool, value: str, json_wanted: bool):
    """The answer, in the words the legacy clients read.

    The status is 1 when there is something to give and 0 when there is
    not, and the value is carried in "request".  It is never 2.
    """
    if json_wanted:
        return JSONResponse(status_code=200,
                            content={"status": 1 if ok else 0, "request": value})
    if ok:
        return PlainTextResponse(status_code=200, content="OK|%s" % value)
    return PlainTextResponse(status_code=200, content=value)


def _the_json_is_wanted(json_1, form_json) -> bool:
    """The legacy clients ask for json with json=1."""
    if json_1 is not None:
        return str(json_1) not in ("", "0", "false", "False")
    if form_json is not None:
        return str(form_json) not in ("", "0", "false", "False")
    return False


@asynccontextmanager
async def _the_lifespan(app):
    """The api gets ready.

    The secrets are taken out of the log here and not when the module
    is loaded: the server sets its own log up after the app is loaded,
    and that would wipe what was done.
    """
    _hide_the_secrets_in_the_log()
    _the_workers()
    threading.Thread(target=_the_janitor, name="captcha-janitor",
                     daemon=True).start()
    logger.info("the captcha api is up, and the key and the addresses are "
                "not written in the log")
    yield


app = FastAPI(
    title="Captcha API",
    version=APP_VERSION,
    description="The captchas are answered on this machine, in a browser.",
    lifespan=_the_lifespan,
)


@app.get("/")
async def the_root():
    return {
        "service": APP_NAME, "version": APP_VERSION,
        "endpoints": ["/in.php", "/res.php", "/health", "/solve",
                      "/solve/recaptcha", "/solve/turnstile", "/stats"],
        "the_words": "the legacy 2captcha protocol: in.php asks, res.php asks again",
        "the_status_is_never": 2,
        "the_methods": sorted(METHODS),
        "for_a_person": "/answer.php (key, id, token) hands the answer of a "
                        "job that was asked for with manual=1",
        "the_limits": {"job_deadline": JOB_DEADLINE, "at_once": MAX_CONCURRENT,
                       "waiting": MAX_QUEUED, "answers_kept": RESULT_TTL},
    }


@app.get("/health")
async def health():
    """Is it up, and what can it do."""
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED, 1),
        "key_is_set": bool(API_KEY),
        "kinds": ["hcaptcha", "recaptcha", "turnstile"],
        "methods": sorted(METHODS),
        "limits": {"job_deadline": JOB_DEADLINE, "at_once": MAX_CONCURRENT,
                   "waiting": MAX_QUEUED, "answers_kept": RESULT_TTL},
        "waiting_now": WORK_QUEUE.qsize(),
        "working_now": len([j for j in JOBS.values() if j.get("state") == "working"]),
        "counts": dict(COUNTS),
    }


@app.get("/stats")
async def stats(x_api_key: str = Header(None), key: str = Query(None)):
    """How many were asked for, and how many were answered."""
    given = x_api_key or key or ""
    if not _the_key_is_right(given):
        raise HTTPException(status_code=401, detail="the key is not right")
    return {"uptime_seconds": round(time.time() - STARTED, 1),
            "counts": dict(COUNTS),
            "waiting_now": WORK_QUEUE.qsize(),
            "working_now": len([j for j in JOBS.values() if j.get("state") == "working"]),
            "jobs_kept": len(JOBS)}


@app.get("/in.php")
@app.post("/in.php")
async def in_php(request: Request, key: str = Query(None),
                 method: str = Query("hcaptcha"),
                 google_sitekey: str = Query(None), googlekey: str = Query(None),
                 sitekey: str = Query(None),
                 pageurl: str = Query(None), url: str = Query(None),
                 proxy: str = Query(None), action: str = Query(None),
                 data: str = Query(None), rqdata: str = Query(None),
                 timeout: int = Query(0), manual: str = Query(None),
                 json_1: int = Query(None, alias="json")):
    """The work is asked for.  The answer comes later, from res.php.

    The work is taken and the caller is let go at once: the answering
    of a captcha takes a browser and a while, and the api has to stay
    quick for the asking and for the asking again.
    """
    body_key = ""
    body_json = None
    try:
        form = await request.form()
        body_key = str(form.get("key") or "")
        body_json = form.get("json")
        method = str(form.get("method") or method)
        google_sitekey = google_sitekey or str(form.get("google_sitekey") or "")
        googlekey = googlekey or str(form.get("googlekey") or "")
        sitekey = sitekey or str(form.get("sitekey") or "")
        # The name of the browser may come with the work, so the
        # browser that answers is the one the request comes from.
        browser = str(form.get("browser") or "")
        pageurl = pageurl or str(form.get("pageurl") or "")
        proxy = proxy or str(form.get("proxy") or "")
        action = action or str(form.get("action") or "")
        data = data or str(form.get("data") or "")
        manual = manual or str(form.get("manual") or "")
    except Exception:
        pass

    json_wanted = _the_json_is_wanted(json_1, body_json)

    # the key comes first: nothing is done for a request that is not
    # allowed to ask
    given = key or body_key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        logger.warning("a request came without the right key")
        return _the_answer(False, ERR_WRONG_USER_KEY, json_wanted)

    # a method that is not known is turned away, and not taken for the
    # hCaptcha it is not
    kind = METHODS.get(str(method or "").strip().lower())
    if not kind:
        logger.info("a method that is not known was asked for")
        return _the_answer(False, ERR_METHOD_NOT_SUPPORTED, json_wanted)

    # google_sitekey is the hCaptcha name, googlekey the reCAPTCHA
    # one, and sitekey the Turnstile one; any of them is taken.
    the_sitekey = google_sitekey or googlekey or sitekey or ""
    the_url = pageurl or url or ""
    if not the_sitekey or not the_url:
        # The sitekey or the page was left out.  This is our own check
        # and it is not about a balance.
        logger.info("the work was asked for without a sitekey or a page")
        return _the_answer(False, ERR_BAD_PARAMETERS, json_wanted)

    # there is only so much that can be done at once, and only so many
    # that can wait
    if WORK_QUEUE.qsize() >= MAX_QUEUED:
        logger.info("there is no room for more work: %d waiting", WORK_QUEUE.qsize())
        return _the_answer(False, ERR_NO_SLOT_AVAILABLE, json_wanted)

    now = time.time()
    job_id = uuid.uuid4().hex
    is_manual = str(manual or "").strip().lower() in ("1", "true", "yes", "on")

    if is_manual:
        # The work is left for a person: some captchas are a picture
        # and need eyes.  The job waits, and the person hands the token
        # in through answer.php.
        _put_job(job_id, {"kind": kind, "sitekey": the_sitekey, "url": the_url,
                          "rqdata": data or rqdata or "", "proxy": proxy or "",
                          "timeout": timeout or 0, "action": action or "",
                          "cdata": "", "done": False, "answer": None,
                          "state": "waiting for a person", "asked_at": now,
                          "deadline": now + JOB_DEADLINE,
        "browser": browser or "",})
        logger.info("the work was asked for and is waiting for a person (job %s)",
                    job_id[:8])
        return _the_answer(True, job_id, json_wanted)

    _put_job(job_id, {"kind": kind, "sitekey": the_sitekey, "url": the_url,
                      "rqdata": data or rqdata or "", "proxy": proxy or "",
                      "timeout": timeout or 0, "action": action or "",
                      "cdata": "", "done": False, "answer": None,
                      "state": "queued", "asked_at": now,
                      "deadline": now + JOB_DEADLINE,
        "browser": browser or "",})

    try:
        WORK_QUEUE.put_nowait(job_id)
    except queue.Full:
        _expire_job(job_id, "there was no room for the work")
        return _the_answer(False, ERR_NO_SLOT_AVAILABLE, json_wanted)

    logger.info("the work was asked for: %s %s (job %s)", kind, the_url[:70],
                job_id[:8])
    return _the_answer(True, job_id, json_wanted)


@app.get("/answer.php")
@app.post("/answer.php")
async def answer_php(request: Request, key: str = Query(None),
                     action: str = Query("set"),
                     job_id: str = Query(None, alias="id"),
                     token: str = Query(None),
                     json_1: int = Query(None, alias="json")):
    """A person hands in the answer to a job that was left for them.

    Only the job that is waiting for a person can be answered this way,
    and only the key of the api may do it.
    """
    body_key = ""
    body_token = ""
    body_id = ""
    try:
        form = await request.form()
        body_key = str(form.get("key") or "")
        body_token = str(form.get("token") or "")
        body_id = str(form.get("id") or "")
    except Exception:
        pass

    json_wanted = _the_json_is_wanted(json_1, None)
    given = key or body_key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        logger.warning("a request came without the right key")
        return _the_answer(False, ERR_WRONG_USER_KEY, json_wanted)

    wanted = job_id or body_id or ""
    the_token = token or body_token or ""
    if not wanted or not the_token:
        return _the_answer(False, ERR_BAD_PARAMETERS, json_wanted)

    with JOBS_LOCK:
        job = JOBS.get(wanted)
        if job is None:
            return _the_answer(False, ERR_WRONG_CAPTCHA_ID, json_wanted)
        if job.get("state") != "waiting for a person":
            # only the work that was left for a person may be answered
            # this way
            return _the_answer(False, ERR_WRONG_CAPTCHA_ID, json_wanted)
        job["answer"] = {"status": "ok", "kind": job.get("kind"),
                         "token": the_token, "elapsed": 0, "by": "a person"}
        job["done"] = True
        job["state"] = "done"
        job["finished_at"] = time.time()

    logger.info("a person answered the job %s", wanted[:8])
    return _the_answer(True, wanted, json_wanted)


@app.get("/res.php")
@app.post("/res.php")
async def res_php(request: Request, key: str = Query(None),
                  action: str = Query("get"),
                  job_id: str = Query(None, alias="id"),
                  request_id: str = Query(None, alias="request"),
                  json_1: int = Query(None, alias="json")):
    """The answer, when there is one."""
    body_key = ""
    body_json = None
    body_id = ""
    try:
        form = await request.form()
        body_key = str(form.get("key") or "")
        body_json = form.get("json")
        body_id = str(form.get("id") or form.get("request") or "")
    except Exception:
        pass

    json_wanted = _the_json_is_wanted(json_1, body_json)

    given = key or body_key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        logger.warning("a request came without the right key")
        return _the_answer(False, ERR_WRONG_USER_KEY, json_wanted)

    # id is the legacy name, request the one this api used before
    wanted = job_id or request_id or body_id or ""
    if not wanted:
        return _the_answer(False, ERR_WRONG_CAPTCHA_ID, json_wanted)

    job = _get_job(wanted)
    if job is None:
        return _the_answer(False, ERR_WRONG_CAPTCHA_ID, json_wanted)

    if not job.get("done"):
        # the work has a time of its own, and when it is past the
        # answer is that it could not be solved
        if time.time() > job.get("deadline", 0):
            _give_up_job(wanted, "the work ran out of time")
        else:
            return _the_answer(False, NOT_READY, json_wanted)

    answer = job.get("answer") or {}
    if answer.get("token"):
        return _the_answer(True, answer["token"], json_wanted)
    return _the_answer(False, ERR_CAPTCHA_UNSOLVABLE, json_wanted)


class SolveIn(BaseModel):
    sitekey: str = Field(..., description="the key of the captcha")
    url: str = Field(..., description="the page the captcha is on")
    rqdata: str = Field("", description="the request data, when the captcha has any")
    proxy: str = Field("", description="the address to ask from, when one is wanted")
    timeout: int = Field(0, description="how long to give it, in seconds")
    action: str = Field("", description="the action of a turnstile")
    cdata: str = Field("", description="the data of a turnstile")


@app.post("/solve")
async def solve(body: SolveIn, request: Request,
                x_api_key: str = Header(None), key: str = Query(None)):
    """An hCaptcha is answered."""
    given = x_api_key or key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        raise HTTPException(status_code=401, detail="the key is not right")
    answer = await _the_token("hcaptcha", body.sitekey, body.url, body.rqdata,
                              body.proxy, body.timeout, body.action, body.cdata)
    if answer.get("token"):
        return answer
    return JSONResponse(status_code=200, content=answer)


@app.post("/solve/recaptcha")
async def solve_recaptcha(body: SolveIn, request: Request,
                           x_api_key: str = Header(None), key: str = Query(None)):
    """A reCAPTCHA is answered."""
    given = x_api_key or key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        raise HTTPException(status_code=401, detail="the key is not right")
    answer = await _the_token("recaptcha", body.sitekey, body.url, body.rqdata,
                              body.proxy, body.timeout, body.action, body.cdata)
    if answer.get("token"):
        return answer
    return JSONResponse(status_code=200, content=answer)


@app.post("/solve/turnstile")
async def solve_turnstile(body: SolveIn, request: Request,
                           x_api_key: str = Header(None), key: str = Query(None)):
    """A Cloudflare Turnstile is answered."""
    given = x_api_key or key or request.headers.get("X-API-Key") or ""
    if not _the_key_is_right(given):
        raise HTTPException(status_code=401, detail="the key is not right")
    answer = await _the_token("turnstile", body.sitekey, body.url, body.rqdata,
                              body.proxy, body.timeout, body.action, body.cdata)
    if answer.get("token"):
        return answer
    return JSONResponse(status_code=200, content=answer)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("CAPTCHA_API_PORT", "8090")),
                log_level="info")
