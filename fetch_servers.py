#!/usr/bin/env python3
"""Stella VPN server aggregator.

Runs every few minutes (GitHub Actions cron). It:

  1. reads the channel list from gcp.txt (the same file the app used),
  2. scans each channel's public web preview for posts inside the 4h window,
  3. downloads the .dark document of each new post through the public
     downloader sites (no account, no captcha),
  4. decrypts it with dark_decode.py and keeps a 4-hour cache,
  5. writes servers.json - the ready-made list the app downloads.

Nothing secret is required: the protocol keys are fixed constants from the
Dark Tunnel format and the downloaders are public.

Environment knobs (optional):
  CHANNEL_LIST_URL   default: klamusto/gcp gcp.txt
  MAX_DL_PER_RUN     default: 12
  MAX_SERVERS        default: 60
  MAX_DL_PER_DAY     default: 200
"""

import concurrent.futures as futures
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import dark_decode

CHANNEL_LIST_URL = os.environ.get(
    "CHANNEL_LIST_URL",
    "https://raw.githubusercontent.com/klamusto/gcp/refs/heads/main/gcp.txt")
LIFE_MS = 4 * 60 * 60 * 1000
FUTURE_MS = 10 * 60 * 1000
MAX_DL_PER_RUN = int(os.environ.get("MAX_DL_PER_RUN", "12"))
MAX_DL_PER_DAY = int(os.environ.get("MAX_DL_PER_DAY", "200"))
MAX_SERVERS = int(os.environ.get("MAX_SERVERS", "60"))
MAX_FILE_BYTES = 600_000
MAX_PAGES = 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TGD = "https://tgvideodownloader.org/api/api"
TGD2 = "https://backend2.tgvideodownloader.org/api/api"
TGDL = "https://telegramdownload.org"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "cache.json")
OUT_PATH = os.path.join(HERE, "servers.json")

POST = re.compile(r'data-post="([^"/]+)/(\d+)"')
TIME = re.compile(r'<time[^>]+datetime="([^"]+)"')
DARK_URI = re.compile(r"darktunnel://[A-Za-z0-9%+/=_\-]+")
B64_BLOB = re.compile(r"[A-Za-z0-9+/]{120,8000}={0,2}")
TAG = re.compile(r"<[^>]+>")

session = requests.Session()
session.headers.update({"User-Agent": UA})


def log(msg):
    print(msg, flush=True)


def now_ms():
    return int(time.time() * 1000)


def parse_iso(value):
    t = (value or "").strip()
    if not t:
        return 0
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def get_text(url, timeout=20, headers=None):
    for attempt in range(2):
        try:
            r = session.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 403):
                return None
            time.sleep(1.5)
        except Exception as e:
            log("GET failed %s: %s" % (url, e))
            time.sleep(1.5)
    return None


def channel_list():
    txt = get_text(CHANNEL_LIST_URL, timeout=25)
    if not txt:
        return []
    out = []
    for line in txt.splitlines():
        line = line.strip().strip("/")
        if not line or line.startswith("#") or line.startswith("http"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_]{3,64}", line):
            out.append(line)
    return out


def strip_tags(raw):
    return html_mod.unescape(TAG.sub(" ", raw)).strip()


def scan_channel(channel, now):
    """Return {sourceKey: {postedAt, hint, inline, hasDoc}} for fresh posts."""
    found = {}
    before = 0
    for page in range(MAX_PAGES):
        url = "https://t.me/s/" + channel + ("?before=%d" % before if before else "")
        page_html = get_text(url)
        if not page_html or "data-post=" not in page_html:
            break
        hits = list(POST.finditer(page_html))
        if not hits:
            break
        for i, m in enumerate(hits):
            start = m.end()
            end = hits[i + 1].start() if i + 1 < len(hits) else min(
                len(page_html), start + 9000)
            block = page_html[start:end]
            posted = 0
            tm = TIME.search(block)
            if tm:
                posted = parse_iso(tm.group(1))
            if not posted or now - posted > LIFE_MS or posted - now > FUTURE_MS:
                continue
            text = strip_tags(block)
            inline = None
            dm = DARK_URI.search(block)
            if dm:
                inline = dm.group(0)
            if inline is None:
                for bm in B64_BLOB.finditer(text[:12000]):
                    blob = bm.group(0)
                    try:
                        dec = dark_decode.b64(blob).decode("utf-8", "ignore")
                        if "outbounds" in dec or "encryptedLockedConfig" in dec:
                            inline = blob
                            break
                    except Exception:
                        pass
            has_doc = "tgme_widget_message_document" in block
            if not has_doc and inline is None:
                continue
            key = "%s/%s" % (channel, m.group(2))
            found[key] = {
                "postedAt": posted,
                "hint": text[:800],
                "inline": inline,
                "hasDoc": has_doc,
            }
        ids = [int(m.group(2)) for m in hits]
        oldest_times = [parse_iso(mm.group(1)) for mm in TIME.finditer(page_html)]
        oldest_times = [t for t in oldest_times if t > 0]
        if not ids:
            break
        before = min(ids)
        if oldest_times and now - min(oldest_times) > LIFE_MS:
            break
        time.sleep(0.1)
    return found


def looks_like_document(text):
    return dark_decode.looks_like_config(text)


def download_tgdl(post_url):
    """telegramdownload.org: 2 hops, token valid 2h, 15 files/IP/day."""
    try:
        r = session.post(TGDL + "/api/file-info",
                         json={"url": post_url, "turnstileToken": ""},
                         headers={"Content-Type": "application/json; charset=utf-8"},
                         timeout=25)
        if r.status_code == 429:
            return None, "tgdl-quota"
        if r.status_code >= 400:
            return None, "tgdl-http-%d" % r.status_code
        info = r.json()
        token = info.get("token", "")
        file_url = info.get("url", post_url)
        name = str(info.get("name", "")).lower()
        size = int(info.get("size", 0) or 0)
        mime = str(info.get("mime_type", "")).lower()
        if not token:
            return None, "tgdl-no-token"
        if size > MAX_FILE_BYTES or name.endswith(".apk") or \
                mime.startswith(("image/", "video/", "audio/")):
            return None, "tgdl-not-config"
        r2 = session.get(TGDL + "/api/download",
                         params={"url": file_url, "token": token}, timeout=30)
        if r2.status_code == 429:
            return None, "tgdl-quota"
        if r2.status_code >= 400:
            return None, "tgdl-dl-%d" % r2.status_code
        if len(r2.content) > MAX_FILE_BYTES:
            return None, "tgdl-too-big"
        text = r2.content.decode("utf-8", "ignore")
        return (text, "") if looks_like_document(text) else (None, "tgdl-not-config")
    except Exception as e:
        return None, "tgdl-%s" % e


def download_tgd(post_url):
    """tgvideodownloader.org: one GET, 10 files/IP/day."""
    for base in (TGD, TGD2):
        try:
            r = session.get(base + "/download", params={"url": post_url},
                            headers={"Accept": "*/*",
                                     "Origin": "https://www.tgvideodownloader.org",
                                     "Referer": "https://www.tgvideodownloader.org/"},
                            timeout=30)
            if r.status_code == 429:
                return None, "tgd-quota"
            if r.status_code >= 400:
                continue
            if len(r.content) > MAX_FILE_BYTES:
                return None, "tgd-too-big"
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype.startswith(("image/", "video/", "audio/")):
                return None, "tgd-media"
            text = r.content.decode("utf-8", "ignore")
            return (text, "") if looks_like_document(text) else (None, "tgd-not-config")
        except Exception as e:
            log("tgd failed %s: %s" % (post_url, e))
    return None, "tgd-error"


def download_post(post_url):
    text, err = download_tgd(post_url)
    if text:
        return text, ""
    if err in ("tgd-quota",):
        text2, err2 = download_tgdl(post_url)
        return (text2, err2) if text2 else (None, err2)
    text2, err2 = download_tgdl(post_url)
    if text2:
        return text2, ""
    return None, err2 or err


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("servers", {})
    data.setdefault("seen", {})
    data.setdefault("dl", {"day": "", "count": 0})
    return data


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def main():
    start = now_ms()
    cache = load_cache()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cache["dl"].get("day") != day:
        cache["dl"] = {"day": day, "count": 0}

    channels = channel_list()
    if not channels:
        log("channel list unavailable; serving cached servers only")
    else:
        log("channels: %d" % len(channels))

    candidates = {}
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(scan_channel, ch, start): ch for ch in channels}
        for job in futures.as_completed(jobs):
            ch = jobs[job]
            try:
                fresh = job.result()
            except Exception as e:
                log("scan %s failed: %s" % (ch, e))
                continue
            if fresh:
                log("%s: %d fresh post(s)" % (ch, len(fresh)))
            for key, info in fresh.items():
                if key not in cache["seen"]:
                    candidates[key] = info

    # Newest first so the daily budget is spent on the most useful posts.
    queue = sorted(candidates.items(), key=lambda kv: -kv[1]["postedAt"])
    daily_left = max(0, MAX_DL_PER_DAY - int(cache["dl"].get("count", 0)))
    limit = min(len(queue), MAX_DL_PER_RUN, daily_left)
    log("new posts: %d, will download: %d" % (len(queue), limit))

    results = {}
    handled = set()

    def work(item):
        key, info = item
        if info.get("inline"):
            return key, info["inline"], ""
        if info.get("hasDoc"):
            text, err = download_post("https://t.me/" + key)
            return key, text, err
        return key, None, "no-doc"

    done = 0
    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(work, item) for item in queue[:limit]]
        for job in futures.as_completed(jobs):
            key, text, err = job.result()
            done += 1
            if not text:
                log("skip %s (%s)" % (key, err))
                # Quota/network errors are retried on the next run; a file
                # that is definitively not a config is remembered as seen.
                if err.endswith("not-config") or err == "no-doc":
                    handled.add(key)
                continue
            info = candidates.get(key, {})
            cfg = dark_decode.dark_to_config(text)
            server = dark_decode.build_server(
                cfg, info.get("hint", ""), key,
                info.get("postedAt", start), now_ms())
            if not server:
                log("skip %s (unsupported config)" % key)
                handled.add(key)
                continue
            server["id"] = hashlib.sha1(key.encode()).hexdigest()[:16]
            results[dark_decode.endpoint_key(server)] = server
            handled.add(key)
            cache["dl"]["count"] = int(cache["dl"].get("count", 0)) + 1
            log("decoded %s (%s)" % (key, server["protocol"]))

    # Merge into the cache: newest copy per endpoint wins.
    for k, v in results.items():
        cache["servers"][k] = v
    for key, info in candidates.items():
        if key in handled:
            cache["seen"][key] = info.get("postedAt", start)

    # Keep only the 4h window, and only recently seen post ids.
    live = {}
    for k, s in cache["servers"].items():
        posted = int(s.get("postedAt", 0))
        if posted <= 0 or start - posted > LIFE_MS or posted - start > FUTURE_MS:
            continue
        live[k] = s
    cache["servers"] = live
    cache["seen"] = {k: v for k, v in cache["seen"].items()
                     if start - int(v) <= 48 * 3600 * 1000}

    servers = sorted(live.values(), key=lambda s: -int(s.get("postedAt", 0)))
    servers = servers[:MAX_SERVERS]
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updatedAt": now_ms(),
        "count": len(servers),
        "servers": servers,
    }
    save_json(OUT_PATH, out)
    save_json(CACHE_PATH, cache)
    log("wrote %s: %d server(s), %d download(s) this run, %.1fs"
        % (OUT_PATH, len(servers), done, (now_ms() - start) / 1000.0))
    return 0 if servers else 0


if __name__ == "__main__":
    sys.exit(main())
