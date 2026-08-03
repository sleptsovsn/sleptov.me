#!/usr/bin/env python3
"""
Controller with GitHub-based approvals.

Env vars required:
- TOKEN              : shared token agents use (Authorization: Bearer TOKEN)
- GITHUB_TOKEN       : GitHub personal access token with repo:issues scope
- GITHUB_REPO        : owner/repo where issues will be created, e.g. sleptsovsn/sleptov.me
- BASE_URL           : public/base URL to present in issue links (optional)

This controller accepts agent reports at /api/report and creates a GitHub Issue
for "check" reports. It polls issue comments for a line starting with /approve
and enqueues an "install" command for the agent which will pick it up via
/api/commands.

Notes:
- This prototype uses in-memory storage (CMD_DB, ISSUE_MAP). Replace with
  persistent storage (SQLite/Redis) for reliability.
- Run behind a reverse proxy with TLS in production.
"""
import os, json, pathlib, threading, time, requests, uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime

REPORT_DIR = pathlib.Path("reports")
REPORT_DIR.mkdir(exist_ok=True)
APP = FastAPI()

TOKEN = os.environ.get("TOKEN", "changeme")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # e.g. sleptsovsn/sleptov.me
BASE_URL = os.environ.get("BASE_URL", "http://192.168.12.151:8000")
POLL_COMMENTS_INTERVAL = int(os.environ.get("POLL_COMMENTS_INTERVAL", "15"))

# in-memory stores (replace with DB for production)
CMD_DB = {}         # agent_id -> [commands]
ISSUE_MAP = {}      # issue_number -> {"agent": agent_id, "path": report_path, "created": ts}


def save_report(agent_id, payload):
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    p = REPORT_DIR / f"{agent_id}_{ts}.json"
    p.write_text(json.dumps(payload, indent=2))
    return str(p)


def gh_create_issue(title: str, body: str):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO must be set")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    payload = {"title": title, "body": body, "labels": ["updates-request"]}
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()  # JSON includes "number" (issue number), "html_url", etc.


def gh_get_issue_comments(issue_number: int):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def enqueue_install(agent_id: str):
    cmd = {"id": str(uuid.uuid4()), "action": "install", "created": datetime.utcnow().isoformat() + "Z"}
    CMD_DB.setdefault(agent_id, []).append(cmd)
    print("Enqueued", cmd, "for", agent_id)
    return cmd


@APP.post("/api/report")
async def api_report(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.split()[1] != TOKEN:
        raise HTTPException(status_code=403, detail="unauth")
    payload = await request.json()
    agent = payload.get("agent", "unknown")
    path = save_report(agent, payload)
    # if this is a check report, create a GitHub issue for approval
    if "check" in payload:
        title = f"[updates] {agent} reported available updates ({datetime.utcnow().isoformat()})"
        body = (
            f"Agent `{agent}` reported available updates.\n\n"
            f"Controller report saved at `{path}`.\n\n"
            f"To approve installation, comment with `/approve` on this issue.\n\n"
            f"Agent ID: `{agent}`\n\n"
            f"Controller URL: {BASE_URL}\n"
        )
        try:
            issue = gh_create_issue(title, body)
            issue_number = issue["number"]
            ISSUE_MAP[issue_number] = {"agent": agent, "path": path, "created": datetime.utcnow().isoformat()}
            print("Created GitHub issue", issue_number, "for", agent)
        except Exception as e:
            print("Failed creating GitHub issue:", e)
    return JSONResponse({"ok": True, "saved": path})


@APP.get("/api/commands")
async def api_commands(agent_id: str = None, request: Request = None):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.split()[1] != TOKEN:
        raise HTTPException(status_code=403, detail="unauth")
    if not agent_id:
        raise HTTPException(status_code=400, detail="need agent_id")
    cmds = CMD_DB.get(agent_id, [])
    return JSONResponse({"commands": cmds})


@APP.post("/api/ack")
async def api_ack(req: Request):
    j = await req.json()
    agent = j.get("agent"); cid = j.get("command_id")
    if agent and cid:
        lst = CMD_DB.get(agent, [])
        CMD_DB[agent] = [c for c in lst if c.get("id") != cid]
    return JSONResponse({"ok": True})


# background thread: poll comments on issues we've created and check for "/approve"
def comments_poller():
    while True:
        try:
            for issue_number, meta in list(ISSUE_MAP.items()):
                try:
                    comments = gh_get_issue_comments(issue_number)
                    for c in comments:
                        body = c.get("body","")
                        if body and body.strip().lower().startswith("/approve"):
                            agent_id = meta["agent"]
                            # enqueue install for that agent
                            enqueue_install(agent_id)
                            # optionally close the issue and post a comment
                            close_url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}"
                            headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
                            requests.patch(close_url, headers=headers, json={"state":"closed"}, timeout=5)
                            comment_url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments"
                            requests.post(comment_url, headers=headers, json={"body":"Approved by controller; install queued on agent."}, timeout=5)
                            print("Approved issue", issue_number, "-> queued install for", agent_id)
                            # remove mapping (we handled it)
                            ISSUE_MAP.pop(issue_number, None)
                            break
                except Exception as e:
                    print("Error polling issue", issue_number, e)
        except Exception as e:
            print("Comments poller top-level error", e)
        time.sleep(POLL_COMMENTS_INTERVAL)


# start poller thread when app launches
@app.on_event("startup")
async def startup_event():
    t = threading.Thread(target=comments_poller, daemon=True)
    t.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("controller_github:APP", host="0.0.0.0", port=8000, reload=True)
