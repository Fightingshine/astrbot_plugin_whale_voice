# 参与贡献

## 开发约定

- 用户界面、说明、错误提示使用简体中文；代码标识符与第三方API名称保留原文。
- 默认不调用模型、不发真实QQ消息；不能用一次发送成功推断所有客户端可播放。
- 不在源码、测试、截图、Issue或日志中放真实密钥、私有音色引用、群号或同学个人信息。
- 不自动覆盖同名音频，不在未命中曲库时偷偷调用付费接口。
- 所有改动要有对应回归，涉及页面必须验证真实渲染与交互。

## 本地运行测试

在仓库根目录执行。脚本支持仓库目录被重命名或GitHub自动添加版本后缀。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/run_tests.py
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe scripts/check_public_tree.py
.\.venv\Scripts\python.exe scripts/check_page.py
```

最后一项需要 Node.js，用于语法检查，不会执行页面脚本。运行时不需要安装开发依赖或Node.js。

Windows上不具备创建符号链接权限时，相关测试会明确跳过；Linux CI与服务器应补跑，不应把“跳过”说成通过。

## 页面验证

使用自制的短测试音频，在隔离协议桩与真实 AstrBot 内嵌页分别检查：上传草稿→修改别名→启停→搜索→试听→下载→重命名→删除。不要在真实班群里试发，也不要修改既有歌曲来做验收。

可运行不带账号、不调用云端API的浏览器协议桩：

```powershell
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-browser.txt
.\.venv\Scripts\python.exe scripts/browser_smoke.py
```

Windows检测到本机Edge时直接使用；其他环境需先安装Playwright Chromium。若需要下载，默认可设置国内镜像后执行：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright/"
.\.venv\Scripts\python.exe -m playwright install chromium
```

如镜像未同步对应版本，请核对镜像状态，不要关闭TLS验证。结果保存在忽略提交的`browser_artifacts/`。

页面协议桩测试不能代替真实后端验收；真实后端验收也不能代替QQ客户端收件和播放测试。证据和限制应分开写清楚。

## 提交前

1. 更新相关测试、CHANGELOG与文档。
2. 涉及数据变更，说明旧数据兼容、备份与失败回滚方式。
3. 运行公开源码检查与发布构建。
4. 不提交`voice-aliases.json`、运行配置、音频或测试产物。
5. 更新运行版本时同步`main.py`、`metadata.yaml`、`pyproject.toml`。

CI会在Windows/Linux与Python 3.10/3.12组合中运行离线回归。标签发布只有验证全部通过后才创建Release。
