# ZCode 标题整理器

本地运行的 ZCode 会话标题整理工具：扫描 ZCode 的两个 SQLite 库，按会话真实内容生成统一的中文短标题，在表格里逐条校对后备份并写回。

数据默认只留在本机；只有生成标题时，会话摘要会发送到你配置的模型服务。

当前版本：`0.2.0`（Windows 10/11 x64）。

## 两种使用方式

### 下载 exe（推荐给普通用户）

1. 从 GitHub Releases 下载 `zcode-title-tidy-0.2.0-win64.zip`。
2. 解压后双击 `zcode-title-tidy.exe`，浏览器会自动打开整理页面。
3. 程序会在后台运行，日志在 `%USERPROFILE%\.zcode-title-tidy\tool.log`。

> exe 未做代码签名，Windows SmartScreen 可能提示。Release 会附 `SHA256SUMS.txt`，可以自行核对。

### 从源码运行

需要 Python 3.10 或更高版本；运行时不依赖第三方 Python 包。

```bash
python tidy_titles.py serve
```

Windows 也可以双击 `启动预览.bat`。前端已经构建在 `web/`，不需要安装 Node。

## 第一次使用

打开页面后点右上角「设置」：

| 字段 | 说明 |
| --- | --- |
| 模型 API 地址 | OpenAI Chat Completions 兼容接口，例如 `https://api.example.com/v1/chat/completions` |
| 模型 | 例如 `deepseek-chat`、`gpt-4o-mini` |
| API Key | 保存在本机配置里，不会返回给页面；服务不需要 key 时可留空 |
| ZCode 数据目录 | 默认 `%USERPROFILE%\.zcode`，填到 `.zcode` 这一层 |
| 测试连接 | 发一个很小的请求确认地址、模型和 key 可用 |

如果使用本机 Magpie 路由：

```text
API 地址：http://127.0.0.1:3425/v1/chat/completions
模型：group/auto-deepseek-v4-1-flash
API Key：留空
```

没有配置模型时，仍然可以手动编辑和写回标题，只是不能自动生成。

## 工作流

1. 点「生成缺失」生成候选；已有候选的行可以用「重新生成选中」重跑。
2. 在表格里对照「现在的标题」和「新标题」，可以直接改，也可以取消勾选。
3. 点「保存方案」。
4. **完全退出 ZCode**：关闭主窗口，并右键托盘图标退出。
5. 点「应用 N 条」；写回前会自动备份，写回后重新读取校验。
6. 重新打开 ZCode，侧栏就是新标题。

写回后标题会被标记为 `title_source=custom`，ZCode 以后不会自动覆盖它们。新会话仍然会按 ZCode 自己的规则自动起名。

### 手动标题的保护

「来源」显示为「手动」的行，默认**不会**被勾选，也不会被「生成缺失」处理。需要改它们时，自己勾选后点「重新生成选中」，或者直接编辑新标题。

### 方案过期

方案保存的是生成候选时的原标题。如果之后在 ZCode 里改了某条标题，重新打开工具时该行会显示红色「方案过期」，应用时直接跳过。重新生成这些行即可。

## 为什么会有超长标题

ZCode 只在第一轮结束后自动起一次标题，要求 3–7 个词。模型调用失败或超时时，会退化成“首条消息截断 60 字”。所以那些很长的标题不是重命名策略的问题，而是生成失败后的兜底。这个工具会重新读会话内容，用更严格的规则生成短标题。

## 安全与数据完整性

- 写回前自动备份两个数据库到 `%USERPROFILE%\.zcode-title-tidy\backups\<时间戳>\`，同时保存 `original.json`、`plan.json` 和 `apply-result.json`。
- 写回在正常情况下用一个事务同时更新两个库，异常会回滚。SQLite 在 WAL 模式下不保证跨附加数据库的断电/崩溃原子性；写回前的完整备份是最后一道保险。
- 写回前同时比对两个库的当前标题和方案里的旧标题；任何一个变了就跳过，不覆盖。
- 应用前检测 `ZCode.exe`；检测失败视为“无法确认”，同样拒绝写入。
- 数据库结构不兼容时只读：界面会标红并禁止生成/写回。
- 本地写入接口需要每次启动生成的 CSRF token，并校验 Host、Origin 和 Content-Type；解析失败的请求直接拒绝。
- 应用/撤销有互斥锁，避免并发写库。

### 撤销这次写回

只恢复这次应用涉及的标题字段，不动之后产生的新会话和消息（先退出 ZCode）：

```bash
python tidy_titles.py revert "%USERPROFILE%\.zcode-title-tidy\backups\20261018-160334"
```

如果某条标题在写回之后又被改过，`revert` 会跳过它并在结果里列出。撤销也会保留 `meta_json` 里与标题无关的字段。

### 整库恢复（灾难恢复，慎用）

```bash
python tidy_titles.py restore "<备份目录>"
```

`restore` 会把两个数据库整体恢复到备份时刻，**备份之后的新会话和消息会被覆盖**。执行前会先自动做一份 `pre-restore-*` 备份。日常撤销标题请用 `revert`。

## 数据与隐私

- 工具读取 `ZCode 数据目录` 下的 `v2/tasks-index.sqlite` 和 `cli/db/db.sqlite`，只在你确认应用时写回。
- 方案、备份、配置、日志都在 `%USERPROFILE%\.zcode-title-tidy`，不会写进程序安装目录。
- 配置文件是 `%USERPROFILE%\.zcode-title-tidy\config.json`。API Key 只存在这里，不会返回给页面，也不会写进仓库。
- 生成标题时，首条/近期用户消息和助手首条回复会发送到配置的模型 API。默认的本机 Magpie 也可能继续转发给上游供应商；敏感会话请先排除，或把 API 指向你信任的本地模型。
- 环境变量可以覆盖配置（优先级最高）：`ZCODE_TITLE_API`、`ZCODE_TITLE_MODEL`、`ZCODE_TITLE_API_KEY`、`ZCODE_TITLE_ZCODE_DIR`、`ZCODE_TITLE_DATA_DIR`。环境变量里的 key 不会被写进配置文件。

## 数据目录

默认：

```text
%USERPROFILE%\.zcode-title-tidy\
  config.json
  plans\latest.json
  backups\<时间戳>\
  tool.log
```

旧版把 `plans/`、`backups/`、`tool.log` 放在程序目录；启动时会自动迁移到上面的用户数据目录。

## 兼容性

工具要求 ZCode 的两个库包含以下表/字段：

- `tasks`：`task_id`、`workspace_key`、`workspace_path`、`title`、`title_overridden`、`archived`、`created_at`、`updated_at`、`task_status`、`deleted`、`meta_json`
- `session`：`id`、`title`、`title_source`、`time_created`、`time_updated`、`task_type`、`parent_id`、`directory`、`time_title_updated`
- `message`、`part`：用于读取会话内容

ZCode 升级后如果结构变了，界面会显示不兼容错误并禁止写入；不会猜字段、不会半写。

## 命令行

```bash
# 启动本地网页
python tidy_titles.py serve

# 只扫描
python tidy_titles.py scan

# 生成全部缺失的候选
python tidy_titles.py generate

# 强制重新生成全部
python tidy_titles.py generate --force --workers 4

# 应用最近一次方案（需先退出 ZCode）
python tidy_titles.py apply --latest

# 字段级撤销
python tidy_titles.py revert "<备份目录>"

# 整库恢复（灾难恢复）
python tidy_titles.py restore "<备份目录>"

# 打开 ZCode
python tidy_titles.py open-zcode
```

可选参数：`--tasks-db`、`--sessions-db`、`--model`、`--port`、`--no-open`。

## 测试

```bash
python tests/test_safety.py
python tests/test_config.py
```

测试全部使用临时合成数据库，不读取真实会话库。覆盖：撤销不影响新会话、不覆盖无关 `meta_json`、旧方案冲突、写入失败回滚、部分跳过、旧格式备份撤销、配置优先级、API Key 不回显、schema 检查。

## 构建

### Windows exe

```bash
python -m pip install pyinstaller
python build_exe.py
```

输出 `dist/zcode-title-tidy.exe`（单文件，无控制台窗口）。前端资源会一起打包。

### 发布 zip

```bash
python scripts/make_release.py
```

输出：

- `release/zcode-title-tidy-0.2.0-win64.zip`：exe + README + LICENSE
- `release/zcode-title-tidy-0.2.0-source.zip`：源码、构建后的前端、测试和构建脚本

脚本会排除 `plans/`、`backups/`、`tool.log`、`config.json`、`node_modules`、`dist/` 等个人或构建产物。

### 改前端

界面在 `web-src/`，使用 Arco Design + React + Vite：

```bash
cd web-src
npm install
npm run build
```

构建结果输出到 `web/`，Python 服务直接托管。

## 许可

MIT，见 `LICENSE`。
