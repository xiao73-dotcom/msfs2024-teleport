#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MSFS2024 瞬移器 —— GitHub 发布助手（走 REST API，不依赖本地 git）

为什么不用 `git push`：
    受限网络下 github.com 可能不可达（git over HTTPS 失败），但 api.github.com 可达。
    本脚本改用 Git Data API 完成「建仓库 / 推源码 / 打标签」，
    再由仓库里的 GitHub Actions 在云端打包并发布 exe（绕开 uploads 域名限制）。

用法：
    python publish.py whoami                            # 验证 token 是否有效
    python publish.py init   --repo msfs2024-teleport   # 创建公开仓库
    python publish.py push   --repo msfs2024-teleport   # 推源码到 main 分支
    python publish.py push   --repo msfs2024-teleport --tag v0.23   # 推源码并打标签（触发云端发布）
    python publish.py status --repo msfs2024-teleport   # 查看最近的工作流运行

Token 读取顺序：
    1) --token 参数
    2) 环境变量 GITHUB_TOKEN / GH_TOKEN
    3) 项目目录下的 .gh_token 文件（已在 .gitignore 中排除）
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
CURL = shutil.which("curl")

# 要推送的文件白名单（相对项目根，顺序无所谓）
INCLUDE = [
    "app.py",
    "sim_bridge.py",
    "strings.py",
    "geocode.py",
    "build.py",
    "build_world.py",
    "make_icon.py",
    "publish.py",
    "requirements.txt",
    "app_icon.ico",
    "world_110m.json",
    "world_famous.json",
    "SimConnect.dll",
    "README.md",
    "LICENSE",
    ".gitignore",
    ".github/workflows/release.yml",
]


# --------------------------------------------------------------------------- #
# HTTP 封装
# --------------------------------------------------------------------------- #
RETRY_STATUS = (429, 500, 502, 503, 504)
RETRY_TRIES = 6


def _once_curl(token, method, path, payload):
    """用 curl 发请求。

    原因：本环境的出口代理对 Python 的 SSL 栈不友好（urllib 稳定报
    SSLEOFError），而 curl 同一时刻 100% 正常。故优先用 curl 作传输层。
    请求体写临时文件再用 --data-binary @file 传入，避免 base64 blob
    超过 Windows 命令行长度上限。
    """
    url = path if path.startswith("http") else API + path
    tmpd = tempfile.mkdtemp(prefix="ghpub-")
    resp_f = os.path.join(tmpd, "resp")
    body_f = os.path.join(tmpd, "body")
    try:
        args = [CURL, "-sS", "-X", method, "-o", resp_f, "-w", "%{http_code}",
                "--max-time", "180", "--retry", "0",
                "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2022-11-28",
                "-H", "User-Agent: msfs-teleport-publisher"]
        if token:
            args += ["-H", "Authorization: Bearer " + token]
        if payload is not None:
            with open(body_f, "wb") as fh:
                fh.write(json.dumps(payload).encode("utf-8"))
            args += ["-H", "Content-Type: application/json",
                     "--data-binary", "@" + body_f]
        args.append(url)
        r = subprocess.run(args, capture_output=True, text=True, timeout=240)
        code_txt = (r.stdout or "").strip()
        if not code_txt.isdigit():
            return 0, {"error": (r.stderr or "curl failed").strip()[:300]}
        status = int(code_txt)
        body = ""
        if os.path.isfile(resp_f):
            with open(resp_f, "rb") as fh:
                body = fh.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body.strip() else {}
        except Exception:  # noqa: BLE001
            parsed = {"raw": body[:400]}
        return status, parsed
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _once_urllib(token, method, path, payload):
    """单次请求（urllib 版，作为 curl 不可用时的兜底）。"""
    url = path if path.startswith("http") else API + path
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "msfs-teleport-publisher",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001
            parsed = {"raw": body[:400]}
        return e.code, parsed
    except Exception as e:  # noqa: BLE001
        return 0, {"error": repr(e)}


def _once(token, method, path, payload):
    """单次请求，返回 (status_code, parsed_json)；连接层异常返回 (0, {...})。"""
    if CURL:
        st, data = _once_curl(token, method, path, payload)
        if st:
            return st, data
        # curl 连接层失败时，用 urllib 再试一次（两条链路的可用性互补）
        st2, data2 = _once_urllib(token, method, path, payload)
        if st2:
            return st2, data2
        return st, data
    return _once_urllib(token, method, path, payload)


def request(token, method, path, payload=None):
    """调用 GitHub REST API，返回 (status_code, parsed_json)。

    受限网络下 api.github.com 偶发 SSL EOF / 5xx（沙盒出口代理不稳定），
    这里对「连接层失败」和「可重试状态码」自动退避重试，避免推送到一半断掉。
    """
    last = (0, {})
    for attempt in range(RETRY_TRIES):
        st, data = _once(token, method, path, payload)
        if st and st < 500 and st != 429:
            return st, data
        last = (st, data)
        if attempt == RETRY_TRIES - 1:
            break
        wait = min(2 ** attempt, 8)
        print("  [~] %s %s -> HTTP %s，%ss 后重试 (%d/%d)"
              % (method, path.split("?")[0], st or "conn-err", wait,
                 attempt + 1, RETRY_TRIES - 1))
        time.sleep(wait)
    return last


def need(token, method, path, payload=None, ok=(200, 201, 204)):
    st, data = request(token, method, path, payload)
    if st not in ok:
        msg = data.get("message") if isinstance(data, dict) else data
        print("  [X] %s %s -> HTTP %s : %s" % (method, path, st, msg))
        detail = data.get("errors") if isinstance(data, dict) else None
        if detail:
            print("      detail:", detail)
        sys.exit(1)
    return data


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def resolve_token(args):
    if args.token:
        return args.token.strip()
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(env):
            return os.environ[env].strip()
    f = os.path.join(HERE, ".gh_token")
    if os.path.isfile(f):
        with open(f, encoding="utf-8") as fh:
            return fh.read().strip()
    print("[X] 未找到 Token：请用 --token，或设置 GITHUB_TOKEN，或在项目目录放 .gh_token 文件")
    sys.exit(1)


def cmd_whoami(token, args):
    st, d = request(token, "GET", "/user")
    if st != 200:
        detail = d.get("message") or d.get("error") or d
        print("[X] 无法通过认证 -> HTTP %s : %s" % (st, detail))
        if st == 0:
            print("    提示：这是网络层失败（未拿到 HTTP 响应），不是 Token 问题，重试即可。")
        elif st == 401:
            print("    提示：Token 无效或已过期/撤销，请重新生成。")
        sys.exit(1)
    print("[OK] 已登录：%s (%s)" % (d.get("login"), d.get("name") or "-"))
    scopes = d.get("_scopes")
    print("     公开仓库数：%s" % d.get("public_repos"))
    return d.get("login")


def cmd_init(token, args):
    owner = args.owner or cmd_whoami(token, args)
    st, d = request(token, "GET", "/repos/%s/%s" % (owner, args.repo))
    if st == 200:
        print("[=] 仓库已存在：%s" % d["html_url"])
        return
    payload = {
        "name": args.repo,
        "description": args.description,
        "private": bool(args.private),
        "has_issues": True,
        "has_wiki": False,
        "auto_init": False,
    }
    d = need(token, "POST", "/user/repos", payload)
    print("[OK] 仓库已创建：%s" % d["html_url"])


def _seed_empty_repo(token, repo_path, branch):
    """先用 Contents API 建一个种子提交，把空仓库的对象库初始化。

    为什么需要：仓库刚创建（无任何提交）时，Git Data API 的 /git/blobs
    会返回 409 "Git Repository is empty."；而 Contents API 可以直接建首提交。
    种子提交用真实的 README，后续正式提交再覆盖其余文件，历史依然干净。
    """
    rel = "README.md"
    local = os.path.join(HERE, rel)
    if not os.path.isfile(local):
        return None
    with open(local, "rb") as fh:
        content = base64.b64encode(fh.read()).decode("ascii")
    st, d = request(token, "PUT", "%s/contents/%s" % (repo_path, rel), {
        "message": "chore: initial commit",
        "content": content,
        "branch": branch,
    })
    if st not in (200, 201):
        msg = d.get("message") if isinstance(d, dict) else d
        print("  [X] 初始化空仓库失败 -> HTTP %s : %s" % (st, msg))
        sys.exit(1)
    sha = d["commit"]["sha"]
    print("  [i] 种子提交 = %s（Contents API 初始化空仓库）" % sha[:10])
    commit = need(token, "GET", "%s/git/commits/%s" % (repo_path, sha))
    return sha, commit["tree"]["sha"]


def cmd_push(token, args):
    owner = args.owner or cmd_whoami(token, args)
    repo_path = "/repos/%s/%s" % (owner, args.repo)
    branch = args.branch

    # 1) 取当前分支的头提交（空仓库则视为首次提交）
    st, ref = request(token, "GET", "%s/git/ref/heads/%s" % (repo_path, branch))
    if st == 200:
        head_sha = ref["object"]["sha"]
        commit = need(token, "GET", "%s/git/commits/%s" % (repo_path, head_sha))
        base_tree = commit["tree"]["sha"]
        parents = [head_sha]
        print("[i] 分支 %s 当前 HEAD = %s" % (branch, head_sha[:10]))
    else:
        base_tree, parents = None, []
        print("[i] 仓库尚无 %s 分支，执行首次提交" % branch)
        seeded = _seed_empty_repo(token, repo_path, branch)
        if seeded:
            head_sha, base_tree = seeded
            parents = [head_sha]

    # 2) 逐个文件建 blob
    print("[i] 上传 %d 个文件..." % len(INCLUDE))
    tree_items = []
    for rel in INCLUDE:
        local = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(local):
            print("  [!] 跳过（本地不存在）：%s" % rel)
            continue
        with open(local, "rb") as fh:
            raw = fh.read()
        d = need(token, "POST", "%s/git/blobs" % repo_path, {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        })
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": d["sha"]})
        print("  + %-36s %7d B" % (rel, len(raw)))

    # 3) 建 tree
    tree_payload = {"tree": tree_items}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    tree = need(token, "POST", "%s/git/trees" % repo_path, tree_payload)

    # 4) 建 commit
    commit = need(token, "POST", "%s/git/commits" % repo_path, {
        "message": args.message,
        "tree": tree["sha"],
        "parents": parents,
    })
    print("[i] commit = %s" % commit["sha"][:10])

    # 5) 更新/创建分支引用
    if parents:
        need(token, "PATCH", "%s/git/refs/heads/%s" % (repo_path, branch), {
            "sha": commit["sha"], "force": False,
        })
    else:
        need(token, "POST", "%s/git/refs" % repo_path, {
            "ref": "refs/heads/%s" % branch, "sha": commit["sha"],
        })
    print("[OK] 已推送到 %s/%s" % (branch, commit["sha"][:10]))

    # 6) 可选：打标签触发工作流
    if args.tag:
        tag = args.tag if args.tag.startswith("v") else "v" + args.tag
        st, _ = request(token, "GET", "%s/git/ref/tags/%s" % (repo_path, tag))
        if st == 200:
            need(token, "PATCH", "%s/git/refs/tags/%s" % (repo_path, tag), {
                "sha": commit["sha"], "force": True,
            })
            print("[i] 标签 %s 已存在，已强制指向新提交" % tag)
        else:
            need(token, "POST", "%s/git/refs" % repo_path, {
                "ref": "refs/tags/%s" % tag, "sha": commit["sha"],
            })
            print("[i] 已创建标签 %s" % tag)
        print("[OK] 云端构建应已被触发，用 `status` 查看进度")
        print("     https://github.com/%s/%s/actions" % (owner, args.repo))


def cmd_status(token, args):
    owner = args.owner or cmd_whoami(token, args)
    d = need(token, "GET", "/repos/%s/%s/actions/runs?per_page=5" % (owner, args.repo))
    runs = d.get("workflow_runs", [])
    if not runs:
        print("[i] 暂无工作流运行记录（可能刚推送，等几秒再试）")
        return
    print("%-12s %-10s %-22s %s" % ("状态", "结论", "触发事件", "链接"))
    for r in runs:
        print("%-12s %-10s %-22s %s" % (
            r.get("status"), r.get("conclusion") or "-",
            r.get("event"), r.get("html_url")))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="MSFS2024 瞬移器 GitHub 发布助手")
    ap.add_argument("--token", help="GitHub PAT（或用环境变量 / .gh_token 文件）")
    ap.add_argument("--owner", help="仓库所有者，默认取 token 对应用户")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("whoami", help="验证 token")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("init", help="创建仓库")
    p.add_argument("--repo", required=True)
    p.add_argument("--description", default="MSFS2024 任意地点起飞工具")
    p.add_argument("--private", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("push", help="推送源码（可顺带打标签）")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--tag", help="同时创建该标签，触发云端发布，如 v0.23")
    p.add_argument("--message", default="chore: sync source from local build")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("status", help="查看最近的工作流运行")
    p.add_argument("--repo", required=True)
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    token = resolve_token(args)
    args.func(token, args)


if __name__ == "__main__":
    main()
