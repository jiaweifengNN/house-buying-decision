#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步本地购房决策文档到 GitHub 仓库 jiaweifengNN/house-buying-decision
走 Git Data API (blobs -> trees -> commits -> update ref)，不使用 git push
（本机 git:// / ssh 协议走不通，只有 api.github.com 可达）。

用法:
    $env:GITHUB_TOKEN = "<PAT>"; python scripts/sync_github.py "提交说明"
"""
import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error

OWNER = "jiaweifengNN"
REPO = "house-buying-decision"
BRANCH = "main"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYNC_EXT = {".md", ".html", ".json", ".py", ".js", ".css", ".gitignore"}
SKIP_DIRS = {".git", ".workbuddy", "__pycache__", "node_modules", ".github"}

# 出网代理
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://127.0.0.1:64122"

TOKEN_FILE = os.path.join(ROOT, ".workbuddy", "secrets", "github_token.txt")

TOKEN = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
if not TOKEN and os.path.exists(TOKEN_FILE):
    TOKEN = open(TOKEN_FILE, encoding="utf-8").read().strip()

API = "https://api.github.com"


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
        urllib.request.HTTPSHandler(),
    )


def api(method, path, payload=None, raw=False, retries=6):
    """调用 GitHub API，带重试。返回 (status, data_or_body_str)"""
    url = API + path
    data = None
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "house-buying-sync",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with _opener().open(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (body if raw else (json.loads(body) if body.strip() else {}))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            # 4xx 不重试（401/403 是权限问题）
            if 400 <= e.code < 500:
                print("  [HTTP %d] %s" % (e.code, body[:600]))
                return e.code, body
            last_err = "HTTP %d %s" % (e.code, body[:300])
        except Exception as e:
            last_err = repr(e)
        time.sleep(1.2 * (i + 1))
    print("  [FAIL] %s %s -> %s" % (method, path, last_err))
    return -1, last_err


def blob_sha(content_bytes):
    """git blob sha1"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(content_bytes))
    h.update(content_bytes)
    return h.hexdigest()


def collect_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SYNC_EXT and fn != ".gitignore":
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "docs update"
    if not TOKEN:
        print("ERROR: 未设置 GITHUB_TOKEN 环境变量")
        sys.exit(2)

    st, me = api("GET", "/user")
    if st != 200:
        print("ERROR: token 校验失败 (%s)" % st)
        sys.exit(2)
    print("token OK: %s" % (me.get("login") if isinstance(me, dict) else me))

    # 1. base ref
    st, ref = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH))
    if st != 200:
        print("ERROR: 取不到 refs/heads/%s (%s)" % (BRANCH, st))
        sys.exit(2)
    base_commit_sha = ref["object"]["sha"]
    print("base commit: %s" % base_commit_sha[:8])

    st, commit = api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_commit_sha))
    if st != 200:
        print("ERROR: 取不到 base commit (%s)" % st)
        sys.exit(2)
    base_tree_sha = commit["tree"]["sha"]

    # 2. 远端 tree（递归）拿到现有 blob sha
    st, tree = api("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (OWNER, REPO, base_tree_sha))
    remote = {}
    if st == 200:
        for it in tree.get("tree", []):
            if it["type"] == "blob":
                remote[it["path"]] = it["sha"]
    print("remote files: %d" % len(remote))

    # 3. 计算差异，创建 blob
    files = collect_files()
    new_blobs = []
    tree_items = []
    changed = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            content = f.read()
        sha = blob_sha(content)
        if remote.get(rel) == sha:
            tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
            continue
        st, b = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO),
                    {"content": content.decode("utf-8"), "encoding": "utf-8"})
        if st not in (200, 201):
            print("ERROR: blob 创建失败 %s (%s)" % (rel, st))
            sys.exit(2)
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": b["sha"]})
        new_blobs.append(rel)
        changed.append(rel)
        print("  + %s" % rel)

    # 远端已删除/本地不存在的文件保持原样（不删），仅提示
    if not changed:
        print("无文件变化，跳过提交。")
        return

    # 4. 建 tree
    st, t = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO),
                {"base_tree": base_tree_sha, "tree": tree_items})
    if st not in (200, 201):
        print("ERROR: tree 创建失败 (%s)" % st)
        sys.exit(2)
    new_tree_sha = t["sha"]

    # 5. commit
    st, c = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                {"message": msg, "tree": new_tree_sha, "parents": [base_commit_sha]})
    if st not in (200, 201):
        print("ERROR: commit 创建失败 (%s)" % st)
        sys.exit(2)
    new_commit_sha = c["sha"]

    # 6. update ref
    st, r = api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, BRANCH),
                {"sha": new_commit_sha})
    if st != 200:
        print("ERROR: ref 更新失败 (%s)" % st)
        sys.exit(2)

    print("")
    print("=== 同步完成 ===")
    print("commit : %s" % new_commit_sha)
    print("files  : %d changed / %d total" % (len(changed), len(files)))
    for x in changed:
        print("   - %s" % x)
    print("url    : https://github.com/%s/%s/commit/%s" % (OWNER, REPO, new_commit_sha))


if __name__ == "__main__":
    main()
