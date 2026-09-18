# 节点仓库配置面板

服务端本地编辑节点仓库配置文件的网页工具。**不提交、不 push** —— 初始化时把仓库
(所有分支) 克隆到服务端本地，网页上选仓库/分支/改环境变量，改动写回本地文件，
页面显示修改后的完整内容，复制或下载即可。

## 部署 (Render)

1. 把本目录推到一个 GitHub 仓库
2. Render → New → Web Service → 选择该仓库
3. 环境:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
   - (可选) `REPOS_DIR` 指定仓库存放目录，默认 `./repos`
4. Deploy 后打开页面 → 点「⚡ 初始化 / 更新仓库」→ 等克隆完成 → 选择仓库卡片进入

> Render 免费实例文件系统是临时的：实例重启后仓库会丢，重新点「初始化 / 更新仓库」即可。
> 想要持久化可以挂 Render Disk（付费）并把 `REPOS_DIR` 指到挂载盘。

## 部署 (DOM Cloud 免费版)

DOM Cloud 免费版**文件系统持久**（1.5GB），克隆一次重启不丢，比 Render 免费版省心；
出站白名单包含全部 GitHub 域名，面板初始化克隆仓库不受防火墙影响。

1. 注册 DomCloud（需要邀请码，或 GitHub 账号绑定: 注册满 6 个月且有 1 个 follower）
2. my.domcloud.co → Create Website → 选 **Clone from Internet** → 粘贴
   `https://github.com/13370463558/node-config-panel`（私有仓库会提示安装 SSH key）
3. 框架/模板选 **Python Flask** 或 **Custom Template**
4. 部署完成后打开 **VS Code / SSH 终端**，确认环境:
   ```bash
   git --version                 # 必须可用 (克隆仓库用)
   pip install -r requirements.txt
   ```
5. 配置 NGINX (面板里 Website → Settings → Nginx 或自定义 recipe):
   ```yaml
   nginx:
     root: public_html/public    # 空目录, 仅作静态根, 防止 app.py 等源码被直接下载
     passenger:
       enabled: on
       app_start_command: gunicorn app:app --bind 127.0.0.1:$PORT --workers 1 --timeout 120
       env_var_list:
         # 仓库放网站根目录之外, 避免被公网直接下载
         - REPOS_DIR=$HOME/repos
   ```
6. 重启网站 → 打开页面 → 点「⚡ 初始化 / 更新仓库」→ 选仓库卡片进入配置

> 免费版限制: 非 x64 服务器 (Flask/gunicorn 纯 Python 不受影响)、2GB 月流量 (面板用量极小)、
> 60 天登录一次自动续期、免费域名 *.dom.my.id 有横幅 (自定义域名无横幅)。

## 本地运行

```bash
pip install -r requirements.txt
export REPOS_DIR=/绝对路径/你的仓库目录   # 可选, 复用已有克隆
python app.py
# 打开 http://localhost:8000
```

## 加仓库

编辑 `app.py` 顶部的 `REPOS` 列表，加一行 `("仓库名", "git 地址")`。

## 解析规则

| 语言 | 模式 | 说明 |
|---|---|---|
| JS/TS | `process.env.X \|\| '默认值'` | 字符串/数字/布尔可改, 表达式只读 |
| Python | `os.environ.get('X', '默认值')` / `os.getenv(...)` | 同上 |
| Go | `os.Getenv("X")` | 只读展示, 用「原始编辑」改 |

找不到模式的文件可用「原始编辑」直接改全文。
