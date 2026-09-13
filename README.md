# 猫娘代码协助 · Catgirl Code Assistance

> 给 [N.E.K.O](https://project-neko.online) 用的本地代码陪伴插件。
> 把项目目录授权给她，然后**用聊天的方式**读代码、问代码、改代码 —— 全程留在本机。

[![CI](https://github.com/HMUG12/n.e.k.o_plugin_catgirl-code-assistance/actions/workflows/ci.yml/badge.svg)](https://github.com/HMUG12/n.e.k.o_plugin_catgirl-code-assistance/actions/workflows/ci.yml)
`N.E.K.O Plugin` · `Python 3.11+` · `零第三方依赖` · `零外发网络请求`

---

## 她能做什么

| 场景 | 你可以直接这样说 |
|---|---|
| 摸清项目 | “这个项目有哪些文件？” / “先看 `core/` 下面有什么” |
| 找东西 | “名字里带 `config` 的 python 文件” / “`handle_login` 在哪被调用过？” |
| 读代码 | “把 `src/auth/login.py` 打开看看” / “只看 120 到 200 行” |
| 看结构 | “这个文件的整体结构是什么？” |
| 讲人话 | “讲讲这个函数干嘛的，我看不懂” |
| 要建议 | “审查一下这个文件，哪些地方能改进？” |
| 改代码 | “把裸 `except` 改掉，先看 diff” → “就按这个改” |
| 反悔 | “撤回刚才那次修改” |
| 记住约定 | “记一下：环境变量在 `deploy/env.sh`” |

支持 Python / C / C++ / Java / Kotlin / Scala / JS / TS / Go / Rust / Swift / PHP / Ruby / Lua / SQL / Shell / 配置文件等常见源码。
其中 Python 走 `ast` 做精确结构解析，其余语言走正则大纲。

---

## 为什么敢说「隐私保留本地」

1. **目录白名单** —— 没写进「可读文件夹」的路径一律拒绝，**包括伪装成路径的符号链接**（同时校验 `abspath` 与 `realpath`）；
2. **本地脱敏** —— 所有返回给模型的文本先在本机过一遍正则：私钥块、AWS Key、GitHub Token、OpenAI Key、Slack Token、Google API Key、JWT、Bearer Token、含账密的连接串、硬编码 `password/secret/api_key=`、邮箱、手机号、身份证、银行卡号 → 替换成 `[REDACTED:XXX]`；
3. **零联网** —— 插件源码里没有任何 HTTP 客户端、没有 `socket`、没有 `subprocess`；CI 里有一条「禁止引入网络/子进程模块」的检查来强制兑现这条承诺；
4. **改动可回滚** —— 每次落盘前先把原文备份到插件私有目录，随时 `undo_edit`。

> 正则脱敏能挡住常见写法，但挡不住刻意把密钥拆开再拼接的做法。
> 真正的安全边界是**目录白名单 + 不联网**。

---

## 三步装好

### 方式 A：直接导入打包好的插件（推荐）

1. 到 [Releases](../../releases) 下载 `catgirl_code_assistance-<version>.neko-plugin`；
2. N.E.K.O → 插件页 → 导入该包；
3. 打开 **猫娘代码协助** 设置面板 → 「可读文件夹」每行填一个绝对路径 → 保存 → 点「重建索引」。

### 方式 B：源码树开发模式（改代码用这个）

```bash
git clone https://github.com/HMUG12/n.e.k.o_plugin_catgirl-code-assistance.git
# 把仓库里的 catgirl_code_assistance/ 整个目录放进 N.E.K.O 源码树的 plugin/plugins/ 下
uv run neko-plugin check catgirl_code_assistance --strict   # 有 CLI 时的官方检查
python tools/check_plugin.py                                # 没 CLI 时的离线等价检查
```
然后在 N.E.K.O 插件详情页Reload。改了代码同样只需 Reload。

> ⚠️ 不要把源码手工复制进用户插件目录（`%LOCALAPPDATA%\N.E.K.O\plugins\<id>\`），
> 也不要建符号链接 —— 这两条都不属于官方开发流程。

---

## 能力一览

| 能力 | 入口 ID | 备注 |
|---|---|---|
| 查看工作区状态 | `get_workspace_status` | 排查「为什么读不到」最快的手段 |
| 列目录 | `list_directory` | 返回条目完整绝对路径 |
| 找文件 | `search_files` | 支持 `*.py`、`*util*` |
| 搜内容 | `grep_code` | 全项目正则，返回行号 + **已脱敏**代码行 |
| 读文件 | `read_code_file` | 支持 `start_line` / `end_line` 片段读 |
| 结构大纲 | `code_outline` | 类 / 函数 / 签名 / 行号 |
| 解释上下文包 | `explain_code` | 内容 + 大纲 + 局部审查一次给全 |
| 代码审查 | `review_code` | 本地静态检查 + 改进建议 |
| 符号定位 | `find_symbol` | 找定义位置 |
| 改动预览 | `propose_edit` | **只读**，出 unified diff，不需要写权限 |
| 应用改动 | `apply_edit` | 需写权限，自动备份 |
| 新建文件 | `create_file` | 需写权限，父目录须存在 |
| 撤销 / 历史 | `undo_edit` / `list_edit_history` | 从本地备份还原 |
| 项目笔记 | `remember_note` / `list_notes` / `forget_note` | 存在本地 store，跨会话 |
| LLM 自动调用 | `cca_code_search`（`@llm_tool`） | 让大模型自己能查代码 |

**故意不做删除文件的入口**：删代码的代价远大于收益，交给你的 IDE / Git。

---

## 审查会报什么

- 硬编码凭据、疑似密钥（高危）
- Python：裸 `except`、`eval/exec`、`subprocess(shell=True)`、可变默认参数、`assert` 做校验、函数过长（>80 行）、嵌套过深（≥5 层）
- C/C++：`strcpy/strcat/gets/sprintf` 等不安全拷贝、`free` 后未置空
- 通用：超长行（>120）、遗留 `TODO/FIXME/HACK/XXX/BUG` 标记

每条都带行号、级别和**可直接执行的修改建议**。

---

## 仓库结构

```
.
├── catgirl_code_assistance/          # 插件源码（目录名 = plugin.id = entry 包名）
│   ├── plugin.toml                   # 清单：元数据 / UI / i18n / 配置段
│   ├── __init__.py                   # 主类：生命周期 + Router 注册 + 定时器
│   ├── core/                         # 纯业务逻辑（不依赖 SDK，可单测）
│   │   ├── config.py                 #   配置模型 + 脏数据安全强转
│   │   ├── pathguard.py              #   目录白名单 / 符号链接越界防护
│   │   ├── redactor.py               #   本地脱敏 + 敏感信息扫描
│   │   ├── reader.py                 #   二进制判定 / 多编码回退 / 行窗口
│   │   ├── analyzer.py               #   大纲 / 符号定位 / 启发式审查
│   │   ├── patch.py                  #   diff / 备份 / 撤销
│   │   ├── indexer.py                #   SQLite 工作区索引
│   │   ├── persona.py  util.py
│   ├── routers/                      # AI 入口（五个业务域）
│   │   ├── workspace.py              #   设置面板 / 索引 / 状态
│   │   ├── explore.py                #   列目录 / 找文件 / 搜内容
│   │   ├── reading.py                #   读 / 大纲 / 解释 / 审查
│   │   ├── editing.py                #   预览 / 落盘 / 撤销
│   │   └── memory.py                 #   项目笔记
│   ├── ui/settings.tsx               # Hosted 设置面板
│   ├── i18n/zh-CN.json  i18n/en.json
│   └── docs/guide.md                 # 用户指南（面板里能看到）
├── tests/                            # 52 个单元测试（含离线 SDK 替身）
├── tools/
│   ├── check_plugin.py               # 离线静态自检（≈ neko-plugin check --strict）
│   ├── build_plugin.py               # 打包成 .neko-plugin
│   └── smoke_package.py              # 交付包冒烟测试
├── .github/workflows/ci.yml          # 云端 CI：自检 + 测试 + 打包 + 冒烟
├── config.example.toml               # 用户运行时配置模板
└── dist/                             # 构建产物（已随仓库发布，方便直接取用）
```

---

## 开发者

```bash
python tools/check_plugin.py      # 静态自检
python -m unittest discover -s tests -v   # 单元测试（52 项）
python tools/build_plugin.py      # 打包 → dist/catgirl_code_assistance-<ver>.neko-plugin
python tools/smoke_package.py     # 对打出来的包做端到端冒烟
```

**为什么测试跑得起来？** `tests/_fake_sdk.py` 按官方 API 语义复刻了一个最小 SDK
（`neko_plugin` / `lifecycle` / `plugin_entry` / `ui.action` / `ui.context` / `llm_tool` /
`Ok` / `Err` / `SdkError` / `NekoPluginBase`），`include_router` 的绑定行为也一并复刻，
所以仓库里不需要 N.E.K.O 源码树也能验证全部入口。

**零依赖证明**：

```bash
uv run --no-project --python 3.13 python -m unittest discover -s tests   # 干净环境，52 项全通过
```

CI 会在 ubuntu 与 windows、Python 3.11 与 3.13 的四个组合上跑完整流程。

---

## 云端部署

插件只依赖 Python 标准库，不写死任何本地绝对路径，也不依赖桌面特有能力，
因此在 N.E.K.O 的云端/服务端形态下同样可用 —— 区别只是「本机」指的是运行 N.E.K.O 的那台机器：

- 在服务端实例上把要读的项目路径填进 `readable_roots`（见 `config.example.toml`）；
- SQLite 索引、备份目录、笔记全部落在**该实例**的插件私有目录内；
- 脱敏与白名单逻辑一致，返回内容依旧不含明文凭据。

---

## 已知边界

- 静态检查通过 ≠ 运行通过：最终请在目标 N.E.K.O 版本里实际加载、打开面板、触发入口、Reload 各验一次；
- 正则脱敏非完备方案（见上文隐私说明）；
- 超大单仓（>20 万文件）建议调低 `index_limit` 或收紧 `allowed_extensions`；
- 二进制文件（图片、音频、已编译产物）会被直接拒绝读取。

---

## 许可

MIT © HMUG12 —— 详见 [LICENSE](LICENSE)。
