import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

API = "https://api.github.com"

def gh(method: str, path: str, token: str, body=None):
    url = f"{API}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "join-org-scan",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except Exception:
            payload = raw
        return e.code, payload

def comment(token, repo, issue_number, body):
    gh("POST", f"repos/{repo}/issues/{issue_number}/comments", token, {"body": body})

def close_issue(token, repo, issue_number):
    gh("PATCH", f"repos/{repo}/issues/{issue_number}", token, {"state": "closed"})

def has_label(issue, name):
    return any(l["name"] == name for l in issue.get("labels", []))

def get_target_from_labels(issue):
    for l in issue.get("labels", []):
        m = re.match(r"^target:(.+)$", l["name"])
        if m:
            return m.group(1)
    return ""

def is_org_member(token, org, username):
    code, _ = gh("GET", f"orgs/{org}/members/{username}", token)
    return code == 204

def add_user_to_team(token, org, team_slug, username):
    return gh("PUT", f"orgs/{org}/teams/{team_slug}/memberships/{username}", token, {"role": "member"})

def is_user_in_team(token, org, team_slug, username):
    code, payload = gh("GET", f"orgs/{org}/teams/{team_slug}/memberships/{username}", token)
    return code == 200 and payload and payload.get("state") in ("active", "pending")

def load_simple_yaml(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    lines = [ln.rstrip("\n") for ln in text.splitlines()]

    def parse_value(v: str):
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip() for p in inner.split(",")]
            out = []
            for p in parts:
                if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
                    out.append(p[1:-1])
                else:
                    out.append(p)
            return out
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        if v in ("true", "false"):
            return v == "true"
        return v

    root = {}
    stack = [(0, root)]
    for ln in lines:
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        # Remove inline comments
        ln = ln.split("#")[0].rstrip()
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        key, _, val = ln.strip().partition(":")
        val = val.strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        cur = stack[-1][1]
        if val == "":
            cur[key] = {}
            stack.append((indent + 2, cur[key]))
        else:
            cur[key] = parse_value(val)
    return root

def list_open_join_issues(token, repo):
    # search API: repo:OWNER/REPO is:issue is:open label:join-request
    q = f"repo:{repo} is:issue is:open label:join-request"
    code, payload = gh("GET", f"search/issues?q={urllib.parse.quote(q)}&per_page=100", token)
    if code != 200:
        raise RuntimeError(f"Search failed: {code} {payload}")
    return payload.get("items", [])

def main():
    # Load configuration first
    cfg = load_simple_yaml(".github/join-config.yml")
    
    # Get environment variables
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("REPO", "").strip()
    
    # Prefer org from environment, fallback to config file
    org = os.environ.get("ORG", "").strip() or cfg.get("org", "").strip()
    
    if not token:
        raise ValueError("Environment variable GH_TOKEN is not set or empty")
    if not org:
        raise ValueError("Organization (org) is not set in environment variable ORG or config file .github/join-config.yml")
    if not repo:
        raise ValueError("Environment variable REPO is not set or empty")

    teams_cfg = cfg.get("teams") or {}

    issues = list_open_join_issues(token, repo)

    for it in issues:
        issue_number = it["number"]
        author = it["user"]["login"]

        target = get_target_from_labels(it)
        if not target or target not in teams_cfg:
            continue

        team_cfg = teams_cfg[target]
        team_slug = team_cfg.get("team_slug", "") or ""

        # If not org member yet, remind and keep open
        if not is_org_member(token, org, author):
            if has_label(it, "invited"):
                comment(token, repo, issue_number,
                        f"@{author} 温馨提示：你还未加入 **@{org}**。请在这里接受邀请：\n\nhttps://github.com/orgs/{org}/invitation")
            continue

        # If needs team, ensure team membership
        if team_slug:
            if not is_user_in_team(token, org, team_slug, author):
                code, payload = add_user_to_team(token, org, team_slug, author)
                if code not in (200, 201):
                    # Can't add team for some reason; leave a note and continue
                    comment(token, repo, issue_number,
                            f"@{author} 已检测到你已加入 **@{org}**，但加入 **@{org}/{team_slug}** 仍失败，将稍后重试。\n\n"
                            f"HTTP {code}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```")
                    continue

        # Now complete: comment + close
        if team_slug:
            comment(token, repo, issue_number, f"@{author} ✅ 已确认你已加入 **@{org}** 并加入 **@{org}/{team_slug}**，本 Issue 将关闭。")
        else:
            comment(token, repo, issue_number, f"@{author} ✅ 已确认你已加入 **@{org}**，本 Issue 将关闭。")

        close_issue(token, repo, issue_number)

if __name__ == "__main__":
    main()