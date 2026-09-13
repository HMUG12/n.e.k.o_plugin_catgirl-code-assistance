# 更新日志

## 1.0.1

修复安装包无法导入的问题。

- **修复**：`.neko-plugin` 包改为官方布局 —— 包根新增 `manifest.toml`（`schema_version` / `package_type` / `id` / `package_name` / `version` / `package_description`）与 `metadata.toml`（payload sha256），插件源码移到 `payload/plugins/catgirl_code_assistance/`，并补齐 `payload/dependencies.toml`、`payload/profiles/default.toml`。
  原先「插件文件直接放包根 + `manifest.json`」的猜测式布局不被官方安装器识别（报「缺少包级 manifest.toml」）。
- **修正**：删除 `plugin.toml` 中的空 `[plugin.dependencies]` 表 —— 官方要求该键只能是「插件 ID 字符串列表」，零依赖时应整段省略。
- **增强**：`tools/smoke_package.py` 现在会校验包结构、包级清单字段、以及按官方算法重算 payload sha256 与 `metadata.toml` 比对。
- **增强**：`tools/check_plugin.py` 新增「`[plugin].dependencies` 必须是列表」「禁止 `requirements.txt`」两条检查。

## 1.0.0

首个正式版本。

**读代码**
- `list_directory` / `search_files` / `grep_code`：列目录、找文件、全项目正则搜索，统一返回绝对路径
- `read_code_file`：支持行窗口读取，自动二进制判定与多编码回退（utf-8 / gbk / big5 / shift_jis …）
- `code_outline`：Python 走 AST，其余语言走正则大纲
- `explain_code`：内容 + 大纲 + 局部审查的一次性上下文包
- `find_symbol`：按名字定位定义

**写/改代码**
- `propose_edit`：只读的 unified diff 预览
- `apply_edit` / `create_file`：两层权限（总开关 + 逐操作授权），写入前自动备份
- `undo_edit` / `list_edit_history`：从本地备份回滚
- 不提供删除入口

**审查**
- `review_code`：硬编码凭据、裸 except、eval/exec、shell=True、可变默认参数、长函数/深嵌套、
  `strcpy` 系列不安全拷贝、超长行、遗留 TODO 标记

**记忆**
- `remember_note` / `list_notes` / `forget_note`：本地 store 持久化，跨会话

**隐私与安全**
- 目录白名单 + `abspath`/`realpath` 双重判定，挡符号链接越界
- 14 类内置脱敏规则 + 自定义正则，命中替换为 `[REDACTED:XXX]`
- 零第三方依赖、零外发网络请求（CI 有硬性检查）
- 写权限默认全部关闭

**工程**
- Router 子包架构（5 个业务域），深模块分层
- 52 项单元测试 + 离线 SDK 替身
- 离线静态自检、打包、交付包冒烟三条 tools 脚本
- CI：ubuntu / windows × Python 3.11 / 3.13 四组合全量验证
