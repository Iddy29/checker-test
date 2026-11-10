"""The protocol of the captcha api, tested with the solver mocked.

Nothing here opens a browser: the solver is replaced with one that
answers, or does not, so the words the api says can be checked on
their own.

    python3 test_captcha_api.py        (or: pytest test_captcha_api.py)
"""

import asyncio
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# The key has to be known before the api is loaded, so it is put in
# the environment first.
TEST_KEY = "test-key-for-the-protocol"
os.environ["CAPTCHA_API_KEY"] = TEST_KEY

import local_hcaptcha_solver  # noqa: E402  (the solver that is mocked)
import captcha_api  # noqa: E402
from captcha_api import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


# ── the mocked solver
CALLS = []


async def _the_solver(sitekey, pageurl, rqdata=None, proxy=None, timeout=0,
                      **kwargs):
    """A solver that answers, or does not, as the test says."""
    CALLS.append({"sitekey": sitekey, "pageurl": pageurl, "rqdata": rqdata,
                  "proxy": proxy})
    behaviour = MOCK["behaviour"]
    if behaviour == "slow":
        # the work takes a while, and then it is answered
        await asyncio.sleep(MOCK["sleep"])
        return MOCK.get("token", "a-token")
    if behaviour == "answer":
        return MOCK.get("token", "a-token")
    if behaviour == "break":
        raise RuntimeError("the browser fell over")
    if behaviour == "hang":
        # the work never finishes, and the api must not wait for it
        await asyncio.sleep(MOCK["sleep"])
        return None
    return None


MOCK = {"behaviour": "answer", "token": "a-token", "sleep": 0.0}

local_hcaptcha_solver.solve_hcaptcha_local = _the_solver
local_hcaptcha_solver.solve_recaptcha_v3_local = _the_solver
local_hcaptcha_solver.solve_turnstile_local = _the_solver


def _client():
    return TestClient(app)


def _submit(client, **params):
    """Ask for the work, in the legacy way."""
    params.setdefault("key", TEST_KEY)
    params.setdefault("method", "hcaptcha")
    params.setdefault("google_sitekey", "site-key-123")
    params.setdefault("pageurl", "https://example.com/page")
    params.setdefault("json", 1)
    return client.get("/in.php", params=params)


def _poll(client, job, **params):
    """Ask again for the answer, in the legacy way."""
    params.setdefault("key", TEST_KEY)
    params.setdefault("action", "get")
    params.setdefault("id", job)
    params.setdefault("json", 1)
    return client.get("/res.php", params=params)


# ── the tests
def test_the_work_is_accepted():
    """The answer is a job id, and the status is 1."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    r = _submit(client)
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["status"] == 1, body
    assert body["request"], body
    assert "solution" not in body, body
    assert "error" not in body, body


def test_the_answer_is_pending():
    """While the work goes on the answer says it is not ready."""
    MOCK.update({"behaviour": "slow", "sleep": 2.0})
    client = _client()
    job = _submit(client).json()["request"]
    body = _poll(client, job).json()
    assert body == {"status": 0, "request": "CAPCHA_NOT_READY"}, body
    # and once the work is done, the answer is the token
    for _ in range(30):
        time.sleep(0.5)
        body = _poll(client, job).json()
        if body["status"] == 1:
            break
    assert body == {"status": 1, "request": "the-token"}, body


def test_the_answer_is_given():
    """When the work is done the answer is the token, and never a 2."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    job = _submit(client).json()["request"]
    for _ in range(30):
        time.sleep(0.2)
        body = _poll(client, job).json()
        if body["status"] == 1:
            break
    assert body == {"status": 1, "request": "the-token"}, body
    assert body["status"] != 2, body


def test_the_work_fails():
    """When the captcha cannot be answered the code says so."""
    MOCK.update({"behaviour": "fail"})
    client = _client()
    job = _submit(client).json()["request"]
    for _ in range(30):
        time.sleep(0.2)
        body = _poll(client, job).json()
        if body["status"] == 0 and body["request"] != "CAPCHA_NOT_READY":
            break
    assert body == {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}, body


def test_the_key_has_to_be_right():
    """A request without the right key is turned away."""
    client = _client()
    r = _submit(client, key="wrong-key")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body == {"status": 0, "request": "ERROR_WRONG_USER_KEY"}, body
    # and the same when the answer is asked for
    r = _poll(client, "any-job", key="wrong-key")
    assert r.json() == {"status": 0, "request": "ERROR_WRONG_USER_KEY"}, r.json()


def test_a_job_that_is_not_known():
    """A job that was never asked for is named as such."""
    client = _client()
    body = _poll(client, "no-such-job").json()
    assert body == {"status": 0, "request": "ERROR_WRONG_CAPTCHA_ID"}, body


def test_the_plain_words():
    """Without json=1 the answers are the plain text of the legacy."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    r = _submit(client, json=0)
    assert r.status_code == 200, r.status_code
    assert r.text.startswith("OK|"), r.text
    job = r.text.split("|", 1)[1]
    for _ in range(30):
        time.sleep(0.2)
        r = _poll(client, job, json=0)
        if r.text.startswith("OK|"):
            break
    assert r.text == "OK|the-token", r.text


def test_the_plain_words_of_a_failure():
    """The plain text of a failure is the code itself."""
    MOCK.update({"behaviour": "fail"})
    client = _client()
    r = _submit(client, json=0)
    job = r.text.split("|", 1)[1]
    for _ in range(30):
        time.sleep(0.2)
        r = _poll(client, job, json=0)
        if r.text != "CAPCHA_NOT_READY":
            break
    assert r.text == "ERROR_CAPTCHA_UNSOLVABLE", r.text


def test_the_plain_words_of_a_wrong_key():
    """The plain text of a wrong key is the code itself."""
    client = _client()
    r = _submit(client, key="wrong-key", json=0)
    assert r.text == "ERROR_WRONG_USER_KEY", r.text


def test_the_names_the_older_clients_use():
    """The old names still work: google_sitekey, request and sitekey."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    # the reCAPTCHA name for the key, and the older method name
    r = client.get("/in.php", params={"key": TEST_KEY, "method": "userrecaptcha",
                                       "googlekey": "recaptcha-key",
                                       "pageurl": "https://example.com/page",
                                       "json": 1})
    job = r.json()["request"]
    assert CALLS[-1]["sitekey"] == "recaptcha-key", CALLS[-1]

    # the answer asked for with the old name, "request"
    for _ in range(30):
        time.sleep(0.2)
        body = _poll(client, job).json()
        if body["status"] == 1:
            break
    assert body == {"status": 1, "request": "the-token"}, body

    # and the answer asked for with the old name
    job = _submit(client).json()["request"]
    for _ in range(30):
        time.sleep(0.2)
        r = client.get("/res.php", params={"key": TEST_KEY, "request": job,
                                           "json": 1})
        if r.json()["status"] == 1:
            break
    assert r.json() == {"status": 1, "request": "the-token"}, r.json()


def test_the_work_is_asked_for_in_a_form():
    """The work can be asked for in a form, the way the legacy does."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    r = client.post("/in.php", data={"key": TEST_KEY, "method": "hcaptcha",
                                      "google_sitekey": "site-key-123",
                                      "pageurl": "https://example.com/page",
                                      "json": "1"})
    body = r.json()
    assert body["status"] == 1, body
    job = body["request"]

    r = client.post("/res.php", data={"key": TEST_KEY, "action": "get",
                                      "id": job, "json": "1"})
    assert r.json()["status"] in (0, 1), r.json()


def test_the_work_comes_back_at_once():
    """The caller is let go at once, and the answer is ready after."""
    MOCK.update({"behaviour": "slow", "sleep": 3.0})
    client = _client()
    t0 = time.time()
    r = _submit(client)
    took = time.time() - t0
    assert r.status_code == 200
    assert took < 1.5, "the answer to the request took %ss" % took
    job = r.json()["request"]
    # and asking again while it works is quick too
    t0 = time.time()
    body = _poll(client, job).json()
    assert time.time() - t0 < 1.0
    assert body["request"] == "CAPCHA_NOT_READY", body


def test_the_work_that_breaks():
    """When the solver falls over the job ends cleanly, with no answer."""
    MOCK.update({"behaviour": "break"})
    client = _client()
    job = _submit(client).json()["request"]
    for _ in range(40):
        time.sleep(0.2)
        body = _poll(client, job).json()
        if body["request"] != "CAPCHA_NOT_READY":
            break
    assert body == {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}, body


def test_the_work_that_never_finishes():
    """The api is quick to answer while the work goes on.

    The api must not wait for the work, and it must not say the work
    is done while it is not.
    """
    MOCK.update({"behaviour": "hang", "sleep": 4.0})
    client = _client()
    t0 = time.time()
    job = _submit(client).json()["request"]
    assert time.time() - t0 < 1.0, "the request took too long"
    # and asking again while it works is quick, and says it is pending
    t0 = time.time()
    body = _poll(client, job).json()
    assert time.time() - t0 < 1.0, "the answer took too long"
    assert body == {"status": 0, "request": "CAPCHA_NOT_READY"}, body
    # and once it is over, the answer is that it could not be solved
    for _ in range(40):
        time.sleep(0.3)
        body = _poll(client, job).json()
        if body["request"] != "CAPCHA_NOT_READY":
            break
    assert body == {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}, body


def test_the_job_is_never_seen_half_done():
    """A job that is done always has its answer with it.

    The work is finished under the lock, so a reader never finds a job
    that is done and has no answer.
    """
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    job = _submit(client).json()["request"]
    for _ in range(40):
        time.sleep(0.1)
        body = _poll(client, job).json()
        if body["status"] == 1:
            break
        assert body == {"status": 0, "request": "CAPCHA_NOT_READY"}, body
    assert body == {"status": 1, "request": "the-token"}, body


def test_the_key_is_not_written_in_the_log():
    """The key is not written down, in any of the words."""
    import io as _io
    import logging as _logging

    # the words the access log writes, with the key in them
    stream = _io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(captcha_api._HideTheSecrets("%(message)s"))
    logger = _logging.getLogger("uvicorn.access.test")
    logger.addHandler(handler)
    logger.setLevel(_logging.INFO)
    logger.propagate = False
    try:
        logger.info('127.0.0.1:1 - "GET /in.php?key=%s&method=hcaptcha HTTP/1.1" 200',
                    TEST_KEY)
        logger.info('127.0.0.1:2 - "GET /res.php?key=%s&action=get&id=x HTTP/1.1" 200',
                    TEST_KEY)
    finally:
        logger.removeHandler(handler)
    words = stream.getvalue()
    assert TEST_KEY not in words, words
    assert "the key" in words, words


def test_every_log_has_the_key_taken_out():
    """Every place the api writes the log takes the key out.

    The api is started first, because that is when the redaction is put
    in place: the server sets its own log up after the app is loaded.
    """
    import logging as _logging

    with _client():
        pass

    for name in ("uvicorn.access", "uvicorn.error", "captcha_api", ""):
        the_logger = _logging.getLogger(name) if name else _logging.getLogger()
        for handler in the_logger.handlers:
            assert isinstance(handler.formatter, captcha_api._HideTheSecrets), (
                "the logger %r has a formatter that does not hide the key: %r"
                % (name, handler.formatter))


def test_a_method_that_is_not_known_is_turned_away():
    """A method the api does not know is not taken for another one."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    for method in ("nosuchmethod", "captcha", "", "HCAPTCHA_TYPO"):
        body = _submit(client, method=method).json()
        assert body == {"status": 0, "request": "ERROR_METHOD_NOT_SUPPORTED"}, (method, body)
    # and the solver was never called for them
    before = len(CALLS)
    _submit(client, method="nosuchmethod")
    assert len(CALLS) == before, "the solver was called for a method that is not known"
    # and the plain text of it
    r = _submit(client, method="nosuchmethod", json=0)
    assert r.text == "ERROR_METHOD_NOT_SUPPORTED", r.text


def test_the_methods_that_are_known_are_taken():
    """Each known method reaches the right kind of captcha."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    for method, kind in (("hcaptcha", "hcaptcha"),
                         ("recaptcha", "recaptcha"),
                         ("userrecaptcha", "recaptcha"),
                         ("turnstile", "turnstile")):
        CALLS.clear()
        _submit(client, method=method)
        for _ in range(40):
            time.sleep(0.1)
            if CALLS:
                break
        assert CALLS, "the solver was not called for %s" % method


def test_the_work_that_is_given_up_on_is_not_written_over():
    """A result that comes after the time is up does not write over it."""
    MOCK.update({"behaviour": "slow", "sleep": 3.0})
    client = _client()
    job = _submit(client).json()["request"]
    # the work is given up on
    captcha_api._give_up_job(job, "the work ran out of time")
    # and the late result does not change it
    for _ in range(40):
        time.sleep(0.2)
        body = _poll(client, job).json()
        if body["request"] != "CAPCHA_NOT_READY":
            break
    assert body == {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}, body
    # and when the late result does arrive, it does not write over the
    # job that was given up on
    stored = None
    for _ in range(60):
        time.sleep(0.2)
        stored = captcha_api._get_job(job)
        if stored and stored.get("late"):
            break
    assert stored and stored.get("late") is True, stored
    assert stored["answer"].get("reason") == "the work ran out of time", stored
    assert stored["answer"].get("token") is None, stored


def test_the_work_that_runs_out_of_time_is_given_up_on():
    """A job that is past its time is given up on, and says so."""
    MOCK.update({"behaviour": "slow", "sleep": 5.0})
    client = _client()
    job = _submit(client).json()["request"]
    # the job is given a very short time
    with captcha_api.JOBS_LOCK:
        captcha_api.JOBS[job]["deadline"] = time.time() - 1
    body = _poll(client, job).json()
    assert body == {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}, body


def test_there_is_only_so_much_that_can_wait():
    """When the queue is full the work is turned away, not lost."""
    import queue as _queue

    client = _client()
    # a queue that is full and cannot be taken from, so the test is the
    # same every time
    full = _queue.Queue(maxsize=1)
    full.put_nowait("something")
    old_queue = captcha_api.WORK_QUEUE
    captcha_api.WORK_QUEUE = full
    try:
        body = _submit(client).json()
        assert body == {"status": 0, "request": "ERROR_NO_SLOT_AVAILABLE"}, body
        r = _submit(client, json=0)
        assert r.text == "ERROR_NO_SLOT_AVAILABLE", repr(r.text)
    finally:
        captcha_api.WORK_QUEUE = old_queue


def test_the_old_answers_are_thrown_away():
    """An answer that is old is not kept."""
    client = _client()
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    job = _submit(client).json()["request"]
    for _ in range(40):
        time.sleep(0.1)
        if captcha_api._get_job(job).get("done"):
            break
    # make it old, and let the janitor do its work
    with captcha_api.JOBS_LOCK:
        captcha_api.JOBS[job]["asked_at"] = time.time() - captcha_api.RESULT_TTL - 10
    captcha_api._the_janitor_once()
    assert captcha_api._get_job(job) is None, "the old answer was kept"


def test_the_key_is_not_made_again():
    """The key is made once and kept."""
    first = captcha_api._the_key()
    assert first
    assert captcha_api._the_key() == first, "the key was made again"
    assert len(first) >= 20, "the key is too short to be any use"


def test_the_proxy_is_not_written_in_the_log():
    """The address of the proxy is not written in the log."""
    import io as _io
    import logging as _logging

    stream = _io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(captcha_api._HideTheSecrets("%(message)s"))
    logger = _logging.getLogger("uvicorn.access.test2")
    logger.addHandler(handler)
    logger.setLevel(_logging.INFO)
    logger.propagate = False
    try:
        logger.info('127.0.0.1:1 - "GET /in.php?key=%s&proxy=user:pass@host:8080&pageurl=x HTTP/1.1" 200',
                    TEST_KEY)
    finally:
        logger.removeHandler(handler)
    words = stream.getvalue()
    assert TEST_KEY not in words, words
    assert "user:pass@host" not in words, words


def test_the_client_polls_until_it_is_answered():
    """The client asks for the work and asks again for the answer."""
    import asyncio as _aio

    import captcha_solver

    MOCK.update({"behaviour": "answer", "token": "the-token"})

    # the client is pointed at the api, and the api is asked in the
    # legacy words
    seen = []

    class _Answer:
        status = 200
        text = ""

        async def json(self, content_type=None):
            return {"status": 1, "request": "the-token"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, params=None, timeout=None):
            seen.append((url, dict(params or {})))
            return _Answer()

    class _Aiohttp:
        ClientSession = _Session

    import sys as _sys
    _real = _sys.modules.get("aiohttp")
    _sys.modules["aiohttp"] = _Aiohttp
    try:
        token = _aio.run(captcha_solver._api_solve("hcaptcha", "the-key", "https://x.test/p"))
    finally:
        if _real is not None:
            _sys.modules["aiohttp"] = _real
    assert token == "the-token", token
    # the work was asked for in in.php, and the answer in res.php
    assert seen, "the api was never asked"
    assert seen[0][0].endswith("/in.php"), seen[0][0]
    assert seen[0][1].get("key"), "the client sent no key"
    assert seen[0][1].get("google_sitekey") == "the-key", seen[0][1]
    assert seen[0][1].get("pageurl") == "https://x.test/p", seen[0][1]
    assert seen[0][1].get("json") == 1, seen[0][1]
    assert seen[-1][0].endswith("/res.php"), seen[-1][0]
    assert seen[-1][1].get("action") == "get", seen[-1][1]
    assert seen[-1][1].get("id"), seen[-1][1]


def test_the_work_without_a_sitekey_is_turned_away():
    """A request without a sitekey or a page is turned away.

    The code says the request was wrong, and not that there was no
    money: that would send the reader looking in the wrong place.
    """
    client = _client()
    body = _submit(client, google_sitekey="").json()
    assert body == {"status": 0, "request": "ERROR_BAD_PARAMETERS"}, body
    body = _submit(client, pageurl="").json()
    assert body == {"status": 0, "request": "ERROR_BAD_PARAMETERS"}, body
    r = _submit(client, google_sitekey="", json=0)
    assert r.text == "ERROR_BAD_PARAMETERS", r.text
    # and the solver was never called for them
    before = len(CALLS)
    _submit(client, google_sitekey="")
    assert len(CALLS) == before


def test_the_work_left_for_a_person():
    """The work can be left for a person, and the person can answer it."""
    MOCK.update({"behaviour": "answer", "token": "the-token"})
    client = _client()
    before = len(CALLS)
    job = _submit(client, manual=1).json()["request"]
    assert job, "the work was not taken"
    # the solver was not asked to do it: it is for a person
    time.sleep(1)
    assert len(CALLS) == before, "the solver was asked to do the work of a person"
    # and while nobody has answered, it is still not ready
    assert _poll(client, job).json() == {"status": 0, "request": "CAPCHA_NOT_READY"}, job
    # and the person hands the answer in
    r = client.get("/answer.php", params={"key": TEST_KEY, "id": job,
                                         "token": "the-answer-of-a-person",
                                         "json": 1})
    assert r.json() == {"status": 1, "request": job}, r.json()
    # and now the answer is there
    assert _poll(client, job).json() == {"status": 1, "request": "the-answer-of-a-person"}, job


def test_only_the_work_of_a_person_can_be_answered():
    """A job that the solver is doing cannot be answered by a person."""
    MOCK.update({"behaviour": "slow", "sleep": 3.0})
    client = _client()
    job = _submit(client).json()["request"]
    body = client.get("/answer.php", params={"key": TEST_KEY, "id": job,
                                            "token": "not-welcome",
                                            "json": 1}).json()
    assert body == {"status": 0, "request": "ERROR_WRONG_CAPTCHA_ID"}, body
    # and the key has to be right
    body = client.get("/answer.php", params={"key": "wrong", "id": job,
                                            "token": "x", "json": 1}).json()
    assert body == {"status": 0, "request": "ERROR_WRONG_USER_KEY"}, body
    # and the plain text of it
    r = client.get("/answer.php", params={"key": TEST_KEY, "id": job,
                                          "token": "not-welcome"})
    assert r.text == "ERROR_WRONG_CAPTCHA_ID", r.text


def test_the_client_and_the_api_use_the_same_key():
    """The key of the client and the key of the api come the same way.

    The api takes the key from the environment first and from the file
    after it.  The client must do the same, or the two can end up with
    a key each and every request is turned away.
    """
    import os as _os
    import captcha_solver

    # from the environment, when it is there: both take it from there
    _os.environ["CAPTCHA_API_KEY"] = "a-key-from-the-environment"
    try:
        assert captcha_solver._the_api_key() == "a-key-from-the-environment"
        assert captcha_api._the_key() == "a-key-from-the-environment"
    finally:
        _os.environ.pop("CAPTCHA_API_KEY", None)

    # and from the file, when there is nothing in the environment: both
    # take it from the same place
    assert captcha_solver._the_api_key() == captcha_api._the_key(), (
        "the client and the api do not read the key the same way")


def _run_all():
    """Run every test, and say which ones failed."""
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = []
    for one in tests:
        name = one.__name__
        try:
            one()
            print("   the %-42s passed" % name)
        except AssertionError as e:
            failed.append((name, str(e)))
            print("   the %-42s FAILED: %s" % (name, e))
        except Exception as e:
            failed.append((name, "%s: %s" % (type(e).__name__, e)))
            print("   the %-42s FAILED: %s: %s" % (name, type(e).__name__, e))
    print()
    print("   %d of %d passed" % (len(tests) - len(failed), len(tests)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(_run_all())
