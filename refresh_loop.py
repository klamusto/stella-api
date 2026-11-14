#!/usr/bin/env python3
"""Keep servers.json fresh every 5 minutes from inside a single Actions job.

GitHub's cron scheduler is best-effort: `*/5` ticks can be delayed or dropped
silently, which is why servers only updated when the workflow was re-run by
hand. This script does not rely on it:

  * it runs fetch_servers.py in a loop (default 9 rounds x 5 minutes),
  * commits/pushes servers.json whenever it changed,
  * immediately queues the next run through the Actions API, using the job's
    own GITHUB_TOKEN (workflow_dispatch is the one event that token may fire),
  * the `schedule` entry in the workflow stays only as a backup restarter.

Environment:
  GITHUB_TOKEN       provided by Actions
  GITHUB_REPOSITORY  provided by Actions (owner/repo)
  REFRESH_SECONDS    interval between rounds (default 300)
  REFRESH_ROUNDS     rounds per job (default 9 -> about 45 minutes)
  REFRESH_WORKFLOW   workflow file name (default update-servers.yml)
  REFRESH_REF        branch to dispatch (default main)
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
TOKEN = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
WORKFLOW = os.environ.get("REFRESH_WORKFLOW", "update-servers.yml")
REF = os.environ.get("REFRESH_REF", "main")
INTERVAL = int(os.environ.get("REFRESH_SECONDS", "300"))
ROUNDS = int(os.environ.get("REFRESH_ROUNDS", "9"))


def log(msg):
    print(msg, flush=True)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def commit_and_push():
    status = git("status", "--porcelain", "--", "servers.json", "cache.json")
    if status.returncode != 0:
        log("git: not a checkout here, skipping commit")
        return
    if not status.stdout.strip():
        log("git: servers.json unchanged")
        return
    git("add", "servers.json", "cache.json")
    msg = "servers: " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = git("commit", "-m", msg)
    if c.returncode != 0:
        log("git: commit failed: " + (c.stderr or c.stdout).strip()[:300])
        return
    for attempt in range(3):
        p = git("push")
        if p.returncode == 0:
            log("git: pushed " + msg)
            return
        log("git: push failed, rebasing (%d)" % (attempt + 1))
        git("pull", "--rebase", "--autostash", "--no-edit")
        time.sleep(5)
    log("git: gave up pushing this round")


def dispatch_next():
    """Queue the successor run now, so the chain never depends on cron."""
    if not REPO or not TOKEN:
        log("dispatch: no repo/token available, falling back to schedule")
        return False
    url = ("https://api.github.com/repos/%s/actions/workflows/%s/dispatches"
           % (REPO, WORKFLOW))
    try:
        r = requests.post(
            url,
            json={"ref": REF},
            timeout=25,
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        log("dispatch: HTTP %d %s" % (r.status_code, r.text[:200].replace("\n", " ")))
        return r.status_code in (201, 204)
    except Exception as e:
        log("dispatch failed: %s" % e)
        return False


def main():
    import fetch_servers

    dispatched = dispatch_next()
    for i in range(ROUNDS):
        started = time.time()
        try:
            fetch_servers.main()
        except Exception as e:
            log("fetch failed: %s" % e)
        try:
            commit_and_push()
        except Exception as e:
            log("commit failed: %s" % e)
        if not dispatched:
            dispatched = dispatch_next()
        if i + 1 < ROUNDS:
            wait = INTERVAL - (time.time() - started)
            if wait > 0:
                log("sleeping %ds (round %d/%d)" % (wait, i + 1, ROUNDS))
                time.sleep(wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
