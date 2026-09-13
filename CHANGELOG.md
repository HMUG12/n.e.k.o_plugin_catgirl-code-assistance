# 更新日志

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
