import json
import os
import re
from pathlib import Path
import urllib.request
import urllib.error

API = "https://api.github.com"

def gh(method: str, path: str, token: str, body=None):
    url = f"{API}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "join-org-action",
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

def add_labels(token, repo, issue_number, labels):
    gh("POST", f"repos/{repo}/issues/{issue_number}/labels", token, {"labels": labels})

def set_assignees(token, repo, issue_number, assignees):
    # POST /repos/{owner}/{repo}/issues/{issue_number}/assignees
    gh("POST", f"repos/{repo}/issues/{issue_number}/assignees", token, {"assignees": assignees})

def get_issue(token, repo, issue_number):
    code, payload = gh("GET", f"repos/{repo}/issues/{issue_number}", token)
    if code != 200:
        raise RuntimeError(f"Fetch issue failed: {code} {payload}")
    return payload

def has_label(issue, name):
    return any(l["name"] == name for l in issue.get("labels", []))

def get_target_from_labels(issue):
    for l in issue.get("labels", []):
        m = re.match(r"^target:(.+)$", l["name"])
        if m:
            return m.group(1)
    return ""

# tiny YAML subset parser for our config
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
                # Remove quotes and strip whitespace
                if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
                    out.append(p[1:-1].strip())
                else:
                    out.append(p.strip())
            return out
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1].strip()
        if v in ("true", "false"):
            return v == "true"
        return v.strip()

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
        key = key.strip()
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

def resolve_user_id(token, username):
    code, user = gh("GET", f"users/{username}", token)
    if code != 200 or not user or "id" not in user:
        raise RuntimeError(f"Resolve user id failed: {code} {user}")
    return int(user["id"])

def is_org_member(token, org, username):
    code, _ = gh("GET", f"orgs/{org}/members/{username}", token)
    return code == 204

def invite_to_org(token, org, invitee_id):
    return gh("POST", f"orgs/{org}/invitations", token, {"invitee_id": invitee_id})

def add_user_to_team(token, org, team_slug, username):
    return gh("PUT", f"orgs/{org}/teams/{team_slug}/memberships/{username}", token, {"role": "member"})

def get_team_members(token, org, team_slug):
    """Get list of team members"""
    code, payload = gh("GET", f"orgs/{org}/teams/{team_slug}/members", token)
    if code != 200:
        return []
    return [member["login"] for member in payload] if payload else []

def get_issue_events(token, repo, issue_number):
    """Get issue events to track who added labels"""
    code, payload = gh("GET", f"repos/{repo}/issues/{issue_number}/events", token)
    if code != 200:
        return []
    return payload or []

def get_issue_comments(token, repo, issue_number):
    """Get all comments on an issue"""
    code, payload = gh("GET", f"repos/{repo}/issues/{issue_number}/comments", token)
    if code != 200:
        return []
    return payload or []

def main():
    token = os.environ["GH_TOKEN"]
    org = os.environ["ORG"]
    repo = os.environ["REPO"]
    issue_number = int(os.environ["ISSUE_NUMBER"])
    author = os.environ["ISSUE_AUTHOR"]
    event_action = os.environ.get("EVENT_ACTION", "")
    event_name = os.environ.get("EVENT_NAME", "")
    label_name = os.environ.get("LABEL_NAME", "")
    comment_body = os.environ.get("COMMENT_BODY", "")
    actor = os.environ.get("ACTOR", "")

    cfg = load_simple_yaml(".github/join-config.yml")
    teams_cfg = cfg.get("teams") or {}

    issue = get_issue(token, repo, issue_number)
    target = get_target_from_labels(issue)
    if not target or target not in teams_cfg:
        comment(token, repo, issue_number, "未识别到目标（需要 `target:<name>` 标签且在配置中存在）。")
        return

    team_cfg = teams_cfg[target]
    mode = team_cfg.get("mode", "auto")
    team_slug = team_cfg.get("team_slug", "") or ""

    # Handle review-needed flow
    if mode == "approval":
        reviewers_cfg = team_cfg.get("reviewers") or {}
        reviewer_users = reviewers_cfg.get("users") or []
        reviewer_teams = reviewers_cfg.get("teams") or []
        
        if event_action == "opened":
            # auto-assign reviewers and @ them
            all_reviewers = list(reviewer_users)
            
            # If we have team reviewers, fetch their members for assignment
            for team in reviewer_teams:
                team = team.strip()  # Ensure no whitespace
                team_members = get_team_members(token, org, team)
                all_reviewers.extend(team_members)
            
            if all_reviewers:
                set_assignees(token, repo, issue_number, all_reviewers)
                #ping = " ".join([f"@{u}" for u in all_reviewers])

                # Build approval requirements message
                requirements = []
                if reviewer_users:
                    requirements.append(f"- 需要以下用户的审批: {', '.join([f'@{u}' for u in reviewer_users])}")
                if reviewer_teams:
                    requirements.append(f"- 需要至少一个来自团队 {', '.join([f'@{org}/{team}' for team in reviewer_teams])} 的成员审批")

                req_msg = "\n".join(requirements)
                comment(
                    token, repo, issue_number,
                    f"**该申请需要审核**\n{req_msg}\n\n请在评论中回复 `/approve` 表示审批通过"
                )
            else:
                comment(token, repo, issue_number, "该申请需要审核，但未配置 reviewers.users 或 reviewers.teams。请维护者补充配置。")

            add_labels(token, repo, issue_number, ["pending-approval"])
            return
        
        # Handle /approve and /reject comments
        if event_name == "issue_comment" and comment_body:
            comment_text = comment_body.strip().lower()

            # Handle /reject command (disabled)
            # if comment_text == "/reject":
            #     # Check if actor is authorized
            #     all_authorized = set(reviewer_users)
            #     for team in reviewer_teams:
            #         team = team.strip()  # Ensure no whitespace
            #         team_members = get_team_members(token, org, team)
            #         all_authorized.update(team_members)
            #
            #     if actor not in all_authorized:
            #         comment(token, repo, issue_number, f"@{actor} 你没有权限拒绝该申请。")
            #         return
            #
            #     comment(token, repo, issue_number, f"已被 @{actor} 拒绝。该申请不会自动邀请。")
            #     add_labels(token, repo, issue_number, ["rejected", "done"])
            #     gh("PATCH", f"repos/{repo}/issues/{issue_number}", token, {"state": "closed"})
            #     return

            # Handle /approve command
            if comment_text == "/approve":
                # Build set of authorized reviewers (normalized to lowercase)
                reviewer_users_lower = [u.lower() for u in reviewer_users]
                all_authorized = set(reviewer_users_lower)
                for team in reviewer_teams:
                    team = team.strip()  # Ensure no whitespace
                    team_members = get_team_members(token, org, team)
                    all_authorized.update([m.lower() for m in team_members])
                
                if actor.lower() not in all_authorized:
                    comment(token, repo, issue_number, f"@{actor} 你没有权限审批该申请。")
                    return
                
                # Get all comments and find who approved (only count authorized reviewers)
                comments = get_issue_comments(token, repo, issue_number)
                approved_by = set()
                for cmt in comments:
                    if cmt.get("body", "").strip().lower() == "/approve":
                        commenter = cmt.get("user", {}).get("login")
                        if commenter and commenter.lower() in all_authorized:
                            approved_by.add(commenter.lower())
                
                # Always add current actor (normalized to lowercase)
                approved_by.add(actor.lower())
                
                # Check if all required users have approved
                users_approved = []
                for user in reviewer_users_lower:
                    if user in approved_by:
                        users_approved.append(user)
                
                # Check if at least one team member has approved (excluding those already in reviewer_users)
                team_members_approved = []
                for team in reviewer_teams:
                    team = team.strip()  # Ensure no whitespace
                    team_members = get_team_members(token, org, team)
                    for member in team_members:
                        # Only count as team approval if not already a required user
                        if member.lower() in approved_by and member.lower() not in reviewer_users_lower:
                            team_members_approved.append(member)
                
                # Determine what's still missing
                missing_users = [u for u in reviewer_users if u.lower() not in approved_by]
                team_approved = len(team_members_approved) > 0
                
                # Validate approval requirements
                if missing_users:
                    missing_list = ', '.join([f'@{u}' for u in missing_users])
                    comment(
                        token, repo, issue_number,
                        f"@{actor} 已审批。还需要以下用户的审批: {missing_list}。"
                    )
                    return
                
                if reviewer_teams and not team_approved:
                    team_list = ', '.join(reviewer_teams)
                    comment(
                        token, repo, issue_number,
                        f"@{actor} 已审批。还需要至少一个来自团队 {team_list} 的成员审批（不包括已在用户列表中的成员）。"
                    )
                    return
                
                # All requirements met - add approved label and proceed
                approval_msg = f"✅ 审批已完成！审批者: {', '.join([f'@{u}' for u in sorted(approved_by)])}\n\n开始处理加入请求..."
                comment(token, repo, issue_number, approval_msg)
                add_labels(token, repo, issue_number, ["approved"])
                # Mark flag to proceed with join request processing
                approval_complete = True
        else:
            approval_complete = False

        # Only proceed if approved label exists or approval just completed
        if not approval_complete and not has_label(issue, "approved"):
            return

    # Auto flow triggers on opened; approval flow triggers only after approved label (handled above)
    if mode == "auto" and event_action != "opened":
        return

    # Always act on issue author only
    username = author

    # Invite if not member
    if not is_org_member(token, org, username):
        invitee_id = resolve_user_id(token, username)
        code, payload = invite_to_org(token, org, invitee_id)
        if code not in (201, 202):
            comment(token, repo, issue_number, f"@{username} 邀请失败：HTTP {code}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```")
            return

        comment(
            token, repo, issue_number,
            f"@{username} 已发出 **@{org}** 组织邀请。请尽快接受邀请：\n\nhttps://github.com/orgs/{org}/invitation"
        )
        add_labels(token, repo, issue_number, ["invited"])
    else:
        comment(token, repo, issue_number, f"@{username} 检测到你已是 **@{org}** 成员。")

    # For vteam: try add to team (may fail until invite accepted)
    if team_slug:
        code, payload = add_user_to_team(token, org, team_slug, username)
        if code in (200, 201):
            comment(token, repo, issue_number, f"@{username} -> **@{org}/{team_slug}**。")
            #add_labels(token, repo, issue_number, ["team-added"])
        else:
            comment(
                token, repo, issue_number,
                f"@{username} 已处理邀请，但加入 **@{org}/{team_slug}** 暂未完成（可能需要先接受 org 邀请）。\n\n"
                f"我们会在每日扫描中自动补全。\n\nHTTP {code}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
            )
            add_labels(token, repo, issue_number, ["wait-scanning"])

if __name__ == "__main__":
    main()