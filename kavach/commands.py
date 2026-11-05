"""Command channel: GitHub issues on your repo.

The dashboard's buttons open (or, with an optional token, create) an issue titled
    kavach: <command> [arg]
Only issues opened by the REPOSITORY OWNER are obeyed; anyone else's are ignored, which matters
because the repo is public. After applying a command the runner comments the result and closes
the issue, so the issue list doubles as an audit trail of every control action.
"""
from __future__ import annotations

import json
import os
import urllib.request

PREFIX = "kavach:"
ALLOWED = {"approve", "reject", "pause", "resume", "flatten", "reset-halt", "limits-accept", "step"}


def _gh(method: str, path: str, body=None):
    tok = os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(f"https://api.github.com{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "kavach"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r) if r.status != 204 else None


def parse(title: str) -> tuple[str, str] | None:
    t = title.strip()
    if not t.lower().startswith(PREFIX):
        return None
    rest = t[len(PREFIX):].strip()
    name, _, arg = rest.partition(" ")
    name = name.lower()
    return (name, arg.strip()) if name in ALLOWED else None


def pending_commands(repo: str | None = None, owner: str | None = None) -> list[dict]:
    repo = repo or os.environ["GITHUB_REPOSITORY"]
    owner = (owner or os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]).lower()
    out = []
    for iss in _gh("GET", f"/repos/{repo}/issues?state=open&per_page=50&sort=created&direction=asc"):
        if "pull_request" in iss:
            continue
        cmd = parse(iss["title"])
        if cmd and iss["user"]["login"].lower() == owner:
            out.append({"number": iss["number"], "name": cmd[0], "arg": cmd[1]})
    return out


def resolve(number: int, message: str, repo: str | None = None):
    repo = repo or os.environ["GITHUB_REPOSITORY"]
    _gh("POST", f"/repos/{repo}/issues/{number}/comments", {"body": f"KAVACH: {message}"})
    _gh("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})
