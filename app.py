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

from flask import Flask, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPOS_DIR = os.environ.get("REPOS_DIR", os.path.join(BASE_DIR, "repos"))

# (仓库名, git 地址) — 想加仓库就加一行
REPOS = [
    ("nodejs-argo",       "https://github.com/eooce/nodejs-argo.git"),
    ("python-xray-argo",  "https://github.com/eooce/python-xray-argo.git"),
    ("serverless-xhttp",  "https://github.com/eooce/serverless-xhttp.git"),
    ("sbx-native",        "https://github.com/eooce/sbx-native.git"),
    ("Sing-box",          "https://github.com/eooce/Sing-box.git"),
    ("node-ws",           "https://github.com/eooce/node-ws.git"),
    ("python-ws",         "https://github.com/eooce/python-ws.git"),
]

ENV_FILE_EXTS = {".js", ".ts", ".py", ".go", ".php", ".sh", ".java", ".json"}
TEXT_EXTS = ENV_FILE_EXTS | {".md", ".txt", ".yml", ".yaml", ".toml", ".env", ".conf"}

app = Flask(__name__)


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
    """切到指定分支 (detached), 丢弃工作区改动"""
    rdir = repo_dir(name)
    # 先丢弃本地改动, 再切分支, 避免 dirty 时 checkout 失败
    run_git(rdir, "checkout", "-f", "--detach", f"origin/{branch}", timeout=180)
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
    返回 (新内容, 错误列表, 更新后的条目列表)
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
    for start, end, new_token in sorted(replacements, reverse=True):
        content = content[:start] + new_token + content[end:]

    # 重新解析, 返回最新条目
    items_new, _ = parse_content_string(content, ext)
    return content, errors, items_new


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
    # 排序: 环境变量文件优先, 且靠根目录的优先
    def rank(f):
        depth = f["path"].count("/")
        return (0 if f["path"].endswith(("index.js", "app.js", "app.py", "index.ts", "main.go", "deno.ts")) else 1, depth)
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
    for name, url in REPOS:
        branches = list_branches(name) if is_cloned(name) else []
        result.append({"name": name, "url": url, "cloned": is_cloned(name), "branches": branches})
    return jsonify({"repos": result})


@app.route("/api/init", methods=["POST"])
def api_init():
    os.makedirs(REPOS_DIR, exist_ok=True)
    logs = []
    for name, url in REPOS:
        ok, msg = clone_repo(name, url)
        logs.append({"name": name, "ok": ok, "msg": msg})
    return jsonify({"logs": logs})


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
    data = request.get_json(force=True)
    name, branch, rel = data.get("repo", ""), data.get("branch", ""), data.get("file", "")
    values = data.get("values", {}) or {}
    checkout_branch(name, branch)
    try:
        content = read_file_text(name, rel)
        full = os.path.join(repo_dir(name), rel)
        items, _ = parse_file(full)
        new_content, errors, items_new = apply_values(content, items, values, os.path.splitext(rel)[1].lower())
        if errors:
            return jsonify({"ok": False, "errors": errors})
        write_file_text(name, rel, new_content)
        return jsonify({"ok": True, "content": new_content, "vars": items_new})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/raw-save", methods=["POST"])
def api_raw_save():
    data = request.get_json(force=True)
    name, branch, rel = data.get("repo", ""), data.get("branch", ""), data.get("file", "")
    content = data.get("content", "")
    checkout_branch(name, branch)
    try:
        write_file_text(name, rel, content)
        full = os.path.join(repo_dir(name), rel)
        items, _ = parse_file(full)
        return jsonify({"ok": True, "content": content, "vars": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """丢弃该文件的本地改动, 恢复为分支原始内容"""
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
