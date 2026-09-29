"""Tiny stdlib HTTP helper with retries, so the pipeline has no third-party deps."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (pm-metrics dashboard)"


def get_json(url, params=None, headers=None, retries=4, timeout=30):
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 4xx other than rate limiting won't get better by retrying
            if e.code != 429 and e.code < 500:
                raise
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries - 1:
                raise
        time.sleep(delay)
        delay *= 2


def post_json(url, body, headers=None, timeout=60):
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
