#!/usr/bin/env python3
"""
sgf_watch.py — watches studentgamesfestival.com for the Best Student Game Awards
2026 results (nominees / finalists / winners) and pushes a notification to your
phone via ntfy.sh.

One invocation = one check. Schedule it hourly with cron / Task Scheduler /
GitHub Actions, or run it with --loop to keep it in the foreground.

Setup:
  1. Install the ntfy app (iOS / Android), subscribe to a topic name only you
     know, e.g. "sgf-2026-enes-8f3k".
  2. export NTFY_TOPIC="sgf-2026-enes-8f3k"
  3. python3 sgf_watch.py            # single check
     python3 sgf_watch.py --test     # verify the push works right now
     python3 sgf_watch.py --loop     # foreground, checks every hour

State lives in sgf_watch_state.json next to the script. Delete it to reset.

No third-party dependencies. Python 3.8+.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BASE = "https://www.studentgamesfestival.com"

# Pages whose visible text we hash and diff every run.
WATCH_PAGES = [
    f"{BASE}/",
    f"{BASE}/programme",
    f"{BASE}/jury",
    f"{BASE}/regulations",
]

# URLs that don't exist yet but very likely will when results land.
# (2025 used /2025nominees and /2025-winners, so these mirror that pattern.)
PROBE_URLS = [
    f"{BASE}/nominees",
    f"{BASE}/2026nominees",
    f"{BASE}/2026-nominees",
    f"{BASE}/winners",
    f"{BASE}/2026winners",
    f"{BASE}/2026-winners",
    f"{BASE}/finalists",
    f"{BASE}/results",
    f"{BASE}/selected-games",
    f"{BASE}/shortlist",
]

SITEMAP = f"{BASE}/sitemap.xml"

# Words that, if they appear in new page text or a new sitemap URL, mean
# "this is probably the announcement".
HOT_WORDS = [
    "nominee", "nominees", "nominated", "nomination",
    "finalist", "finalists", "shortlist", "short list",
    "winner", "winners", "laureate",
    "selected games", "selection results", "qualified",
    "results announced", "the results are", "we have selected",
]

WATCH_DAYS = 15
CHECK_INTERVAL_SECONDS = 3600
USER_AGENT = "Mozilla/5.0 (compatible; sgf-watch/1.0; personal results checker)"
TIMEOUT = 30

STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sgf_watch_state.json"
)

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")  # optional, for private servers


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

def fetch(url, timeout=TIMEOUT):
    """Return (status_code, text). status 0 means a network-level failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.getcode(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log(f"  ! fetch failed for {url}: {e}")
        return 0, ""


class TextExtractor(HTMLParser):
    """Pulls visible text out of HTML, ignoring script/style/nav noise."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def visible_text(html):
    parser = TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = parser.text()
    # Squarespace injects cache-busting ids and timestamps; strip digits-heavy
    # noise so the hash doesn't churn on every single request.
    text = re.sub(r"\b[0-9a-f]{16,}\b", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Notification
# ----------------------------------------------------------------------------

def notify(title, message, priority="default", tags="bell", click=None):
    if not NTFY_TOPIC:
        log("!! NTFY_TOPIC is not set — printing instead of pushing.")
        log(f"   [{title}] {message}")
        return False

    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
        "Priority": priority,
        "Tags": tags,
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    req = urllib.request.Request(
        url, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ok = 200 <= resp.getcode() < 300
            log(f"  -> ntfy push {'sent' if ok else 'returned ' + str(resp.getcode())}")
            return ok
    except Exception as e:
        log(f"  ! ntfy push failed: {e}")
        return False


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"! state file unreadable ({e}); starting fresh")
    return {
        "started": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pages": {},          # url -> {"hash": ..., "len": ...}
        "sitemap_urls": [],
        "alive_probes": [],
        "alerted": False,
        "runs": 0,
        "last_run": None,
    }


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------

def find_hot_words(text):
    low = text.lower()
    hits = []
    for word in HOT_WORDS:
        if word in low:
            hits.append(word)
    return hits


def extract_snippet(text, words, radius=220):
    """Grab the neighbourhood of the first matched keyword, for the push body."""
    low = text.lower()
    for w in words:
        i = low.find(w)
        if i >= 0:
            start = max(0, i - radius // 2)
            return " ".join(text[start:start + radius].split())
    return " ".join(text[:radius].split())


def check_sitemap(state, findings):
    status, body = fetch(SITEMAP)
    if status != 200 or not body:
        log(f"  sitemap: unavailable (status {status})")
        return
    urls = sorted(set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)))
    known = set(state.get("sitemap_urls", []))
    if not known:
        state["sitemap_urls"] = urls
        log(f"  sitemap: baseline recorded ({len(urls)} urls)")
        return
    new = [u for u in urls if u not in known]
    if new:
        state["sitemap_urls"] = urls
        log(f"  sitemap: {len(new)} new url(s)")
        for u in new:
            hot = find_hot_words(u)
            findings.append({
                "kind": "new-page",
                "url": u,
                "hot": bool(hot),
                "detail": f"New page on the site: {u}",
            })
    else:
        log(f"  sitemap: unchanged ({len(urls)} urls)")


def check_probes(state, findings):
    alive = set(state.get("alive_probes", []))
    for url in PROBE_URLS:
        status, body = fetch(url)
        if status == 200 and url not in alive:
            alive.add(url)
            findings.append({
                "kind": "probe-live",
                "url": url,
                "hot": True,
                "detail": f"A results page just went live: {url}",
            })
            log(f"  probe: {url} is now LIVE")
        time.sleep(random.uniform(0.4, 1.0))  # be polite
    state["alive_probes"] = sorted(alive)


def check_pages(state, findings):
    pages = state.setdefault("pages", {})
    for url in WATCH_PAGES:
        status, html = fetch(url)
        if status != 200 or not html:
            log(f"  page: {url} -> status {status}, skipped")
            continue
        text = visible_text(html)
        h = content_hash(text)
        prev = pages.get(url)
        pages[url] = {"hash": h, "len": len(text)}

        if prev is None:
            log(f"  page: {url} baseline recorded")
            continue
        if prev["hash"] == h:
            log(f"  page: {url} unchanged")
            continue

        hot = find_hot_words(text)
        delta = len(text) - prev["len"]
        log(f"  page: {url} CHANGED ({delta:+d} chars, keywords: {hot or 'none'})")
        findings.append({
            "kind": "page-changed",
            "url": url,
            "hot": bool(hot),
            "detail": (
                f"Content changed ({delta:+d} chars)."
                + (f" Keywords: {', '.join(hot[:4])}.\n\n{extract_snippet(text, hot)}"
                   if hot else "")
            ),
        })
        time.sleep(random.uniform(0.4, 1.0))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run_once(state):
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
    first_run = state["runs"] == 1

    log(f"--- check #{state['runs']} ---")
    findings = []
    check_sitemap(state, findings)
    check_probes(state, findings)
    check_pages(state, findings)

    if first_run:
        save_state(state)
        notify(
            "SGF 2026 watcher armed",
            "Baseline captured. You'll get a push the moment the results page "
            "appears or the site text changes.",
            priority="low",
            tags="eyes",
            click=BASE,
        )
        log("baseline saved; watcher armed")
        return

    hot = [f for f in findings if f["hot"]]
    warm = [f for f in findings if not f["hot"]]

    if hot:
        body = "\n\n".join(f["detail"] for f in hot[:3])
        notify(
            "🏆 SGF 2026 — results may be up!",
            body[:1800],
            priority="urgent",
            tags="trophy,rotating_light",
            click=hot[0]["url"],
        )
        state["alerted"] = True
    elif warm:
        body = "\n\n".join(f["detail"] for f in warm[:3])
        notify(
            "SGF 2026 site changed",
            body[:1800] + "\n\nNo results keywords yet — worth a look.",
            priority="default",
            tags="mag",
            click=warm[0]["url"],
        )
    else:
        log("no changes")

    save_state(state)


def expired(state):
    try:
        started = dt.datetime.fromisoformat(state["started"])
    except Exception:
        return False
    age = dt.datetime.now(dt.timezone.utc) - started
    return age.days >= WATCH_DAYS


def main():
    ap = argparse.ArgumentParser(description="Watch SGF 2026 for results.")
    ap.add_argument("--loop", action="store_true",
                    help="stay in the foreground and check every hour")
    ap.add_argument("--test", action="store_true",
                    help="send a test notification and exit")
    ap.add_argument("--status", action="store_true",
                    help="print current state and exit")
    args = ap.parse_args()

    if args.test:
        ok = notify(
            "SGF 2026 watcher — test",
            "If you can read this on your phone, notifications work.",
            priority="default",
            tags="white_check_mark",
            click=BASE,
        )
        sys.exit(0 if ok else 1)

    state = load_state()

    if args.status:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        sys.exit(0)

    if expired(state):
        log(f"watch window of {WATCH_DAYS} days has elapsed — nothing to do.")
        log("delete sgf_watch_state.json to start a fresh window.")
        sys.exit(0)

    if args.loop:
        log(f"loop mode: checking every {CHECK_INTERVAL_SECONDS // 60} minutes "
            f"for {WATCH_DAYS} days. Ctrl+C to stop.")
        try:
            while not expired(state):
                run_once(state)
                jitter = random.uniform(-180, 180)
                time.sleep(max(60, CHECK_INTERVAL_SECONDS + jitter))
        except KeyboardInterrupt:
            log("stopped by user")
        notify("SGF 2026 watcher stopped",
               "The 15-day watch window ended (or you stopped it).",
               priority="low", tags="stop_sign")
    else:
        run_once(state)


if __name__ == "__main__":
    main()
