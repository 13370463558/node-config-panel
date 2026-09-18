#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
节点仓库配置面板 — 服务端本地编辑 + 预览复制
=============================================
- 初始化: 把配置的仓库(所有分支)克隆到 REPOS_DIR
- 选仓库 → 选分支 → 解析该分支配置文件里的环境变量 (process.env / os.environ)
- 改值 → 写回本地文件 (不提交, 不 push) → 页面显示修改后的完整内容供复制

部署: Render Web Service (Python), 启动命令 gunicorn app:app
环境变量:
  REPOS_DIR   (可选) 仓库存放目录, 默认 ./repos
"""

import os
import re
import subprocess
import json
from datetime import timedelta

from flask import (Flask, request, jsonify, render_template,
                   session, redirect, url_for)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPOS_DIR = os.environ.get("REPOS_DIR", os.path.join(BASE_DIR, "repos"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PANEL_FILE = os.path.join(DATA_DIR, "panel.json")
LOCK_FILE = os.path.join(DATA_DIR, "locks.json")   # 锁定记录: {"repo|branch": {"文件路径": {"VAR": 值}}}
REPOS_FILE = os.path.join(DATA_DIR, "repos.json")  # 仓库配置: [{name, url, files}]
DEFAULT_PASSWORD = "admin"
SESSION_DAYS = 60   # 登录 cookie 有效期: 两个月

# 默认仓库 (首次启动无 repos.json 时使用; 之后可在面板里增删管理)
REPOS_DEFAULT = [
    {"name": "nodejs-argo",       "url": "https://github.com/eooce/nodejs-argo.git"},
    {"name": "python-xray-argo",  "url": "https://github.com/eooce/python-xray-argo.git"},
    {"name": "serverless-xhttp",  "url": "https://github.com/eooce/serverless-xhttp.git"},
    {"name": "sbx-native",        "url": "https://github.com/eooce/sbx-native.git"},
    {"name": "Sing-box",          "url": "https://github.com/eooce/Sing-box.git"},
    {"name": "node-ws",           "url": "https://github.com/eooce/node-ws.git"},
    {"name": "python-ws",         "url": "https://github.com/eooce/python-ws.git"},
]


def load_repos():
    """读取仓库配置列表 [{name, url, files?}], 无配置时返回默认列表"""
    if os.path.exists(REPOS_FILE):
        try:
            with open(REPOS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return list(REPOS_DEFAULT)


def save_repos(repos):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPOS_FILE, "w", encoding="utf-8") as f:
        json.dump(repos, f, indent=2, ensure_ascii=False)


def get_repo_files(name):
    """该仓库配置的优先解析文件列表 (多语言仓库手动指定用)"""
    for r in load_repos():
        if r.get("name") == name:
            files = r.get("files") or []
            return [str(f).strip() for f in files if str(f).strip()]
    return []

ENV_FILE_EXTS = {".js", ".ts", ".py", ".go", ".php", ".sh", ".java", ".json"}
TEXT_EXTS = ENV_FILE_EXTS | {".md", ".txt", ".yml", ".yaml", ".toml", ".env", ".conf"}

app = Flask(__name__)


# ==================== 认证配置 ====================
def load_panel_config():
    """读取/初始化 data/panel.json: {secret_key, password_hash}
    首次启动自动生成 secret_key, 默认密码 admin
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = {}
    if os.path.exists(PANEL_FILE):
        try:
            with open(PANEL_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    changed = False
    if not cfg.get("secret_key"):
        cfg["secret_key"] = os.urandom(32).hex()
        changed = True
    if not cfg.get("password_hash"):
        cfg["password_hash"] = generate_password_hash(DEFAULT_PASSWORD)
        changed = True
    if changed:
        save_panel_config(cfg)
    return cfg


def save_panel_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PANEL_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_locks():
    """读取锁定记录: {"repo|branch": {"文件路径": {"VAR": 值}}}"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_locks(locks):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(locks, f, indent=2, ensure_ascii=False)


_panel_cfg = load_panel_config()
app.secret_key = _panel_cfg["secret_key"]
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)


@app.before_request
def require_login():
    """除登录页和静态文件外, 全部页面/API 都需要登录"""
    if request.path == "/login" or request.path.startswith("/static"):
        return None
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(load_panel_config()["password_hash"], pw):
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="密码错误"), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/change-password", methods=["POST"])
def api_change_password():
    data = request.get_json(force=True) or {}
    old = data.get("old_password", "")
    new = data.get("new_password", "")
    cfg = load_panel_config()
    if not check_password_hash(cfg["password_hash"], old):
        return jsonify({"ok": False, "error": "原密码错误"})
    if len(new) < 4:
        return jsonify({"ok": False, "error": "新密码至少 4 位"})
    cfg["password_hash"] = generate_password_hash(new)
    save_panel_config(cfg)
    return jsonify({"ok": True, "msg": "密码已修改"})


# ==================== git 工具 ====================
def run_git(repo_dir, *args, timeout=120):
    """在仓库目录执行 git, 返回 (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(
            ["git", "-C", repo_dir] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "git 命令超时"


def repo_dir(name):
    return os.path.join(REPOS_DIR, name)


def is_cloned(name):
    return os.path.isdir(os.path.join(repo_dir(name), ".git"))


def list_branches(name):
    """返回该仓库的所有远端分支名 (不含 origin/ 前缀)"""
    rc, out, _ = run_git(repo_dir(name), "branch", "-r")
    if rc != 0:
        return []
    branches = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "HEAD" in line:
            continue
        if line.startswith("origin/"):
            line = line[len("origin/"):]
        branches.append(line)
    return branches


def checkout_branch(name, branch):
    """切到指定分支 (detached)。

    优先切到本地 panel-<branch> 分支 (含面板锁定写回的 commit, 不 push);
    若当前 HEAD 已在该分支 -> 跳过 checkout, 保留工作区改动
    (用户锁定写入的本地值不能被 git checkout 冲掉);
    切换分支时才强制 checkout 丢弃上一个分支的改动。
    """
    rdir = repo_dir(name)
    local = f"panel-{branch}"
    rc_l, _, _ = run_git(rdir, "rev-parse", "--verify", local)
    ref = local if rc_l == 0 else f"origin/{branch}"
    rc, head, _ = run_git(rdir, "rev-parse", "HEAD")
    rc2, remote, _ = run_git(rdir, "rev-parse", ref)
    if rc == 0 and rc2 == 0 and head.strip() == remote.strip():
        return head.strip()
    run_git(rdir, "checkout", "-f", "--detach", ref, timeout=180)
    return run_git(rdir, "rev-parse", "--short", "HEAD")[1].strip()


def clone_repo(name, url):
    """克隆单个仓库 (含所有分支), 已存在则 fetch 更新"""
    rdir = repo_dir(name)
    os.makedirs(REPOS_DIR, exist_ok=True)
    if is_cloned(name):
        rc, out, err = run_git(rdir, "fetch", "origin", "--prune", timeout=300)
        return (rc == 0), f"fetch: {out.strip() or err.strip() or 'ok'}"
    rc, out, err = run_git(REPOS_DIR, "clone", "--no-single-branch", url, name, timeout=600)
    if rc == 0:
        return True, "克隆完成"
    # 失败时清理半成品目录
    shutil_rmtree(rdir)
    return False, err.strip() or out.strip()


def shutil_rmtree(path):
    import shutil
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# ==================== 环境变量解析 ====================
JS_VAR_RE = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")
PY_VAR_RE = re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*([^)]*)\)")
GO_VAR_RE = re.compile(r"os\.Getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)")
NUMBER_RE = re.compile(r"^-?\d+$|^-?\d*\.\d+$")


def _classify_token(token):
    """把默认值 token 分类: (kind, value, quote)
    kind: string / int / float / bool / expr(只读)
    """
    token = token.strip()
    if token == "":
        return "string", "", "'"
    if token[0] in ("'", '"'):
        q = token[0]
        # 找到匹配的结束引号 (考虑反斜杠转义)
        inner = []
        i = 1
        while i < len(token):
            ch = token[i]
            if ch == "\\" and i + 1 < len(token):
                inner.append(token[i:i + 2])
                i += 2
                continue
            if ch == q:
                break
            inner.append(ch)
            i += 1
        return "string", "".join(inner), q
    low = token.lower()
    if low in ("true", "false"):
        return "bool", token, None
    if NUMBER_RE.match(token):
        return "float" if ('.' in token) else "int", token, None
    return "expr", token, None


def _line_comment(line, quote_chars=("'", '"'), comment_start="//"):
    """找行尾注释文本 (跳过字符串内部的 //)"""
    in_q = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_q:
            if ch == "\\":
                i += 2
                continue
            if ch == in_q:
                in_q = None
        elif ch in quote_chars:
            in_q = ch
        elif line.startswith(comment_start, i):
            return line[i + len(comment_start):].strip()
        i += 1
    return ""


def _dedupe_vars(found):
    """同名变量去重: 保留第一个可编辑项 (回退链里的只读副本丢弃)"""
    seen = {}
    result = []
    for it in found:
        k = it["key"]
        if k in seen:
            prev = seen[k]
            if prev["readonly"] and not it["readonly"]:
                idx = result.index(prev)
                result[idx] = it
                seen[k] = it
            continue
        seen[k] = it
        result.append(it)
    return result


def parse_file(path):
    """解析一个配置文件, 返回 [ {key, kind, value, quote, comment, readonly, span} ]"""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return [], content

    found = []
    lines = content.splitlines(keepends=True)
    offset = 0
    for line in lines:
        line_no = offset
        if ext in (".js", ".ts"):
            matches = list(JS_VAR_RE.finditer(line))
            fallback = False  # 同行中位于某变量回退链(|| 之后)的匹配只读
            for m in matches:
                key = m.group(1)
                after = line[m.end():]
                # 找 || 之后的默认值 (遇到 // 注释或行尾结束)
                pipe = after.find("||")
                comment = _line_comment(after, ("'", '"'), "//")
                if fallback:
                    found.append({
                        "key": key, "kind": "expr", "value": "", "quote": None,
                        "comment": comment, "readonly": True, "span": None,
                    })
                    continue
                if pipe == -1:
                    kind, value, quote = "expr", "", None
                    token_start = token_end = m.end()
                    token = ""
                else:
                    seg = after[pipe + 2:]
                    # 去掉注释部分
                    ci = seg.find("//") if comment else -1
                    if ci != -1:
                        seg = seg[:ci]
                    # 去掉行尾语句分号/逗号 (字符串内部的不会被 rstrip 误删)
                    token = seg.strip().rstrip(";, ").strip()
                    kind, value, quote = _classify_token(token)
                    # 默认值 token 在整行中的绝对位置
                    seg_start = pipe + 2
                    token_start = line_no + m.end() + seg_start + seg.find(token)
                    token_end = token_start + len(token)
                    fallback = True  # 后面的都在回退链里, 只读
                readonly = (kind == "expr") or (not token and pipe == -1)
                found.append({
                    "key": key, "kind": kind, "value": value, "quote": quote,
                    "comment": comment, "readonly": readonly,
                    "span": (token_start, token_end) if not readonly else None,
                })
        elif ext == ".py":
            for m in PY_VAR_RE.finditer(line):
                key = m.group(1)
                token = m.group(2).strip()
                kind, value, quote = _classify_token(token)
                comment = _line_comment(line[m.end():], ("'", '"', '"""'), "#")
                readonly = kind == "expr"
                token_start = line_no + m.start(2) + line[m.start(2):].find(token)
                token_end = token_start + len(token)
                found.append({
                    "key": key, "kind": kind, "value": value, "quote": quote,
                    "comment": comment, "readonly": readonly,
                    "span": (token_start, token_end) if not readonly else None,
                })
        elif ext == ".go":
            for m in GO_VAR_RE.finditer(line):
                found.append({
                    "key": m.group(1), "kind": "expr", "value": "", "quote": None,
                    "comment": "", "readonly": True, "span": None,
                })
        offset += len(line)
    return _dedupe_vars(found), content


def apply_values(content, items, values, ext=""):
    """把 values {key: new_value} 写回 content; items 为 parse_file 返回的条目
    返回 (新内容, 错误列表, 更新后的条目列表, 高亮区间列表)
    高亮区间 = 替换后的 token 在最终内容中的 [start, end)
    """
    errors = []
    replacements = []  # (start, end, new_token)
    for it in items:
        if it["readonly"] or it["span"] is None:
            continue
        key = it["key"]
        if key not in values:
            continue
        new_val = values[key]
        if isinstance(new_val, str):
            new_val = new_val.strip()
        # 值未变化 -> 跳过, 不产生替换和高亮
        if str(new_val) == str(it["value"]):
            continue
        start, end = it["span"]
        if it["kind"] == "string":
            q = it["quote"] or "'"
            escaped = new_val.replace("\\", "\\\\").replace(q, "\\" + q)
            replacements.append((start, end, f"{q}{escaped}{q}"))
        elif it["kind"] in ("int", "float"):
            if not NUMBER_RE.match(str(new_val)):
                errors.append(f"{key}: 需要数字, 收到 '{new_val}'")
                continue
            replacements.append((start, end, str(new_val)))
        elif it["kind"] == "bool":
            low = str(new_val).lower()
            if low not in ("true", "false"):
                errors.append(f"{key}: 需要 true/false, 收到 '{new_val}'")
                continue
            replacements.append((start, end, low))
    # 从后往前替换, 避免偏移错乱
    # 高亮区间: 按原始 start 升序遍历, 累加前面替换造成的长度偏移
    modified = []
    offset = 0
    for start, end, new_token in sorted(replacements):
        final_start = start + offset
        offset += len(new_token) - (end - start)
        modified.append([final_start, final_start + len(new_token)])
    modified.sort()
    for start, end, new_token in sorted(replacements, reverse=True):
        content = content[:start] + new_token + content[end:]

    # 重新解析, 返回最新条目
    items_new, _ = parse_content_string(content, ext)
    return content, errors, items_new, modified


def parse_content_string(content, ext):
    """直接对字符串解析 (apply 后刷新用)"""
    import io
    # 简化: 复用行解析逻辑
    found = []
    lines = content.splitlines(keepends=True)
    offset = 0
    for line in lines:
        if ext in (".js", ".ts"):
            matches = list(JS_VAR_RE.finditer(line))
            fallback = False
            for m in matches:
                key = m.group(1)
                after = line[m.end():]
                pipe = after.find("||")
                comment = _line_comment(after, ("'", '"'), "//")
                if fallback:
                    found.append({
                        "key": key, "kind": "expr", "value": "", "quote": None,
                        "comment": comment, "readonly": True, "span": None,
                    })
                    continue
                if pipe == -1:
                    kind, value, quote = "expr", "", None
                    token = ""
                else:
                    seg = after[pipe + 2:]
                    ci = seg.find("//") if comment else -1
                    if ci != -1:
                        seg = seg[:ci]
                    token = seg.strip().rstrip(";, ").strip()
                    kind, value, quote = _classify_token(token)
                    fallback = True
                readonly = (kind == "expr") or (not token and pipe == -1)
                found.append({
                    "key": key, "kind": kind, "value": value, "quote": quote,
                    "comment": comment, "readonly": readonly, "span": None,
                })
        elif ext == ".py":
            for m in PY_VAR_RE.finditer(line):
                token = m.group(2).strip()
                kind, value, quote = _classify_token(token)
                comment = _line_comment(line[m.end():], ("'", '"', '"""'), "#")
                found.append({
                    "key": m.group(1), "kind": kind, "value": value, "quote": quote,
                    "comment": comment, "readonly": kind == "expr", "span": None,
                })
        elif ext == ".go":
            for m in GO_VAR_RE.finditer(line):
                found.append({
                    "key": m.group(1), "kind": "expr", "value": "", "quote": None,
                    "comment": "", "readonly": True, "span": None,
                })
        offset += len(line)
    return _dedupe_vars(found), content


def scan_env_files(name, branch):
    """切到分支后扫描工作树, 返回:
    {
      env_files: [{path, vars}],
      text_files: [所有文本文件路径, 供原始编辑],
    }
    """
    checkout_branch(name, branch)
    rdir = repo_dir(name)
    env_files = []
    text_files = []
    for root, dirs, files in os.walk(rdir):
        # 跳过 .git
        dirs[:] = [d for d in dirs if d != ".git"]
        if root.count(os.sep) - rdir.count(os.sep) > 3:
            dirs[:] = []
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, rdir)
            text_files.append(rel)
            if ext in ENV_FILE_EXTS:
                items, _ = parse_file(full)
                if items:
                    env_files.append({"path": rel, "vars": items})
    # 排序: 仓库手动配置的优先解析文件 > 常见配置文件名 > 其他; 同级别靠根目录优先
    pref = get_repo_files(name)
    def rank(f):
        depth = f["path"].count("/")
        pri = 0 if f["path"] in pref else (
            1 if f["path"].endswith(("index.js", "app.js", "app.py", "index.ts", "main.go", "deno.ts")) else 2)
        return (pri, depth)
    env_files.sort(key=rank)
    text_files.sort()
    return env_files, text_files


def read_file_text(name, rel):
    rdir = repo_dir(name)
    full = os.path.realpath(os.path.join(rdir, rel))
    if not full.startswith(os.path.realpath(rdir) + os.sep):
        raise ValueError("非法路径")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_file_text(name, rel, content):
    rdir = repo_dir(name)
    full = os.path.realpath(os.path.join(rdir, rel))
    if not full.startswith(os.path.realpath(rdir) + os.sep):
        raise ValueError("非法路径")
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return content


# ==================== API ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    result = []
    for r in load_repos():
        name = r["name"]
        branches = list_branches(name) if is_cloned(name) else []
        result.append({"name": name, "url": r["url"], "cloned": is_cloned(name), "branches": branches})
    return jsonify({"repos": result})


@app.route("/api/init", methods=["POST"])
def api_init():
    os.makedirs(REPOS_DIR, exist_ok=True)
    logs = []
    for r in load_repos():
        name, url = r["name"], r["url"]
        ok, msg = clone_repo(name, url)
        logs.append({"name": name, "ok": ok, "msg": msg})
    return jsonify({"logs": logs})


@app.route("/api/repos-config", methods=["GET"])
def api_repos_config():
    return jsonify({"ok": True, "repos": load_repos()})


@app.route("/api/repos-config", methods=["POST"])
def api_repos_config_save():
    data = request.get_json(force=True) or {}
    repos = data.get("repos") or []
    clean = []
    for r in repos:
        name = str(r.get("name", "")).strip()
        url = str(r.get("url", "")).strip()
        files = r.get("files") or []
        if isinstance(files, str):
            files = [x.strip() for x in files.split(",") if x.strip()]
        files = [str(f).strip() for f in files if str(f).strip()]
        if name and url:
            clean.append({"name": name, "url": url, "files": files})
    save_repos(clean)
    return jsonify({"ok": True, "repos": clean})


@app.route("/api/update", methods=["POST"])
def api_update():
    """更新单个仓库到最新, 并把锁定过的变量值重新写回文件。

    每个有锁定的分支: 重置到本地 panel-<branch> (指向远端最新) ->
    应用锁定值写文件 -> 本地 commit (不 push)。切分支不丢锁定值。
    """
    data = request.get_json(force=True) or {}
    name = data.get("repo", "")
    if not is_cloned(name):
        return jsonify({"ok": False, "error": "仓库未克隆, 先初始化"})
    rc, _, err = run_git(repo_dir(name), "fetch", "origin", "--prune", timeout=300)
    if rc != 0:
        return jsonify({"ok": False, "error": f"fetch 失败: {err[:200]}"})

    branches = list_branches(name)
    locks = load_locks()
    applied = []
    for br in branches:
        rdir = repo_dir(name)
        # 强制切到本地 panel-<branch> (重置为远端最新)
        run_git(rdir, "checkout", "-f", "-B", f"panel-{br}", f"origin/{br}", timeout=180)
        for rel, kv in (locks.get(f"{name}|{br}") or {}).items():
            try:
                full = os.path.join(rdir, rel)
                content = read_file_text(name, rel)
                items, _ = parse_file(full)
                new_content, errors, _, _ = apply_values(
                    content, items, kv, os.path.splitext(rel)[1].lower())
                if errors:
                    applied.append(f"⏭️ {br}/{rel}: {errors[0]}")
                    continue
                write_file_text(name, rel, new_content)
                applied.append(f"✅ {br}/{rel}: " + ", ".join(f"{k}={v}" for k, v in kv.items()))
            except Exception as e:
                applied.append(f"⚠️ {br}/{rel}: {e}")
        # 有改动就本地 commit (不 push), 防止切分支丢失锁定值
        rc_s, out_s, _ = run_git(rdir, "status", "--porcelain")
        if rc_s == 0 and out_s.strip():
            run_git(rdir, "add", "-A")
            rc_c, _, err_c = run_git(rdir, "-c", "user.name=panel",
                                     "-c", "user.email=panel@local",
                                     "commit", "-m", "panel: apply locked values")
            if rc_c != 0:
                applied.append(f"⚠️ {br}: 本地 commit 失败 ({err_c.strip()[:100]})")
    return jsonify({"ok": True, "branches": branches, "applied": applied})


@app.route("/api/branch")
def api_branch():
    name = request.args.get("repo", "")
    branch = request.args.get("branch", "")
    if not is_cloned(name):
        return jsonify({"ok": False, "error": "仓库未克隆, 先初始化"})
    env_files, text_files = scan_env_files(name, branch)
    return jsonify({"ok": True, "branch": branch, "env_files": env_files, "text_files": text_files})


@app.route("/api/file")
def api_file():
    """读取指定文件内容 (需先进入分支)"""
    name = request.args.get("repo", "")
    branch = request.args.get("branch", "")
    rel = request.args.get("file", "")
    checkout_branch(name, branch)
    try:
        content = read_file_text(name, rel)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    items, _ = parse_file(os.path.join(repo_dir(name), rel))
    return jsonify({"ok": True, "content": content, "vars": items})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    """替换变量默认值: write=false 纯预览(不写文件), write=true 写入本地文件。
    不 checkout, 只改单个文件, 避免 partial clone 卡顿。"""
    data = request.get_json(force=True)
    name, branch, rel = data.get("repo", ""), data.get("branch", ""), data.get("file", "")
    values = data.get("values", {}) or {}
    write = bool(data.get("write", False))
    try:
        content = read_file_text(name, rel)
        full = os.path.join(repo_dir(name), rel)
        items, _ = parse_file(full)
        new_content, errors, items_new, modified = apply_values(
            content, items, values, os.path.splitext(rel)[1].lower())
        if errors:
            return jsonify({"ok": False, "errors": errors})
        if write:
            write_file_text(name, rel, new_content)
            # 记录锁定值 (更新仓库后用于写回)
            locks = load_locks()
            key = f"{name}|{branch}"
            locks.setdefault(key, {}).setdefault(rel, {}).update(values)
            save_locks(locks)
        return jsonify({"ok": True, "content": new_content, "vars": items_new,
                        "modified": modified, "saved": write})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "文件不存在, 请先进入该分支再试"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """(保留) 丢弃该文件的本地改动, 恢复为分支原始内容"""
    data = request.get_json(force=True)
    name, branch, rel = data.get("repo", ""), data.get("branch", ""), data.get("file", "")
    checkout_branch(name, branch)
    rc, _, err = run_git(repo_dir(name), "checkout", "-f", "--", rel)
    if rc != 0:
        return jsonify({"ok": False, "error": err})
    try:
        content = read_file_text(name, rel)
        items, _ = parse_file(os.path.join(repo_dir(name), rel))
        return jsonify({"ok": True, "content": content, "vars": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/obfuscate-python", methods=["POST"])
def api_obfuscate_python():
    """Python 代码混淆 (与 obf.eooce.com 同款: zlib 压缩 -> base64 -> 反转 -> exec)

    纯本地实现, 不依赖外部接口。压缩级别 9 (最大压缩, 与线上输出更接近)。
    """
    import zlib
    import base64
    data = request.get_json(force=True) or {}
    code = data.get("code", "")
    if not code.strip():
        return jsonify({"ok": False, "error": "代码为空"})
    compressed = zlib.compress(code.encode("utf-8"), 9)
    b64 = base64.b64encode(compressed)
    rev = b64[::-1].decode("ascii")
    obfuscated = ("_ = (lambda __: __import__('zlib').decompress(__import__('base64').b64decode(__[::-1]))); "
                  f"exec(_('{rev}'))")
    return jsonify({"ok": True, "obfuscated": obfuscated,
                    "original_len": len(code), "obfuscated_len": len(obfuscated)})


@app.route("/api/download-obfuscated", methods=["POST"])
def api_download_obfuscated():
    """打包混淆结果压缩包: 仅含 混淆后的文件 + 该分支的依赖文件 (requirements.txt / package.json)"""
    import io
    import zipfile
    from flask import Response
    data = request.get_json(force=True) or {}
    name, branch, rel = data.get("repo", ""), data.get("branch", ""), data.get("file", "")
    obf = data.get("code", "")
    if not obf.strip():
        return jsonify({"ok": False, "error": "混淆结果为空"})
    checkout_branch(name, branch)   # HEAD 相同则跳过, 不丢锁定值
    rdir = repo_dir(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) 混淆后的文件 (保持原文件名)
        zf.writestr(rel.split("/")[-1], obf)
        # 2) 该分支的依赖文件 (存在才加)
        for dep in ("requirements.txt", "package.json"):
            full = os.path.join(rdir, dep)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        zf.writestr(dep, f.read())
                except Exception:
                    pass
    buf.seek(0)
    fname = f"{name}-{branch.replace('/', '_')}-obfuscated.zip"
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
