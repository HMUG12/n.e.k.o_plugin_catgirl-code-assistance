/**
 * 设置面板的类型声明（仅开发期辅助，不参与打包运行）。
 *
 * 面板真正的数据结构由 Python 端 `@ui.context(id="workspace")` 返回，
 * 这里把它写清楚，方便写 TSX 时有补全、也方便 review。
 */
export type WorkspaceConfig = {
  readable_roots: string[]
  blocked_dirs: string[]
  allowed_extensions: string[]
  max_file_kb: number
  max_matches: number
  max_context_lines: number
  follow_symlinks: boolean
  enable_write: boolean
  allow_create: boolean
  allow_modify: boolean
  allow_delete: boolean
  redact_sensitive: boolean
  custom_redact_patterns: string[]
  index_enabled: boolean
  index_limit: number
  max_undo_steps: number
  reply_style: "gentle" | "professional" | "tsundere"
}

export type RootInfo = {
  path: string
  exists: boolean
  is_dir: boolean
}

export type WorkspaceStatus = {
  ready: boolean
  frozen: boolean
  root_count: number
  roots: RootInfo[]
  index_files: number
  index_bytes: number
  index_backend: "sqlite" | "live"
  index_error: string
  last_scan_at: number
  scanning: boolean
  write_enabled: boolean
  redaction_enabled: boolean
  undo_depth: number
  reply_style: string
  note_count: number
}

export type WorkspaceState = {
  config: WorkspaceConfig
  status: WorkspaceStatus
}
