/**
 * 猫娘代码协助 —— 设置面板
 *
 * UI 纪律（反 AI 味）：单一主操作（保存）、无嵌套卡片、无装饰性光晕、
 * 文案具体到「会发生什么」，颜色只用组件自带的色阶。
 */
import {
  Page, Card, Section, Stack, Text, Divider,
  Switch, Input, Textarea, Select, StatCard, ActionButton, Field, Alert, StatusBadge, Tip,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"
import { useLocalState } from "@neko/plugin-ui"

type State = {
  config: {
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
    reply_style: string
  }
  status: {
    ready: boolean
    frozen: boolean
    root_count: number
    index_files: number
    index_bytes: number
    index_backend: string
    last_scan_at: number
    scanning: boolean
    write_enabled: boolean
    undo_depth: number
    note_count: number
  }
}

function fmtSize(bytes: number): string {
  if (!bytes || bytes <= 0) return "0 B"
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(2) + " GB"
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(2) + " MB"
  if (bytes >= 1024) return (bytes / 1024).toFixed(2) + " KB"
  return bytes + " B"
}

function fmtTime(ts: number): string {
  if (!ts || ts <= 0) return "尚未建立"
  return new Date(ts * 1000).toLocaleString()
}

function lines(list: string[] | undefined): string {
  return (list || []).join("\n")
}

function toList(text: string): string[] {
  return text.split("\n").map((s: string) => s.trim()).filter(Boolean)
}

export default function SettingsPanel(props: PluginSurfaceProps<State>) {
  const { state } = props

  // 缓存 actions —— 首次渲染时 actions 可能为空，否则按钮会消失
  const [cachedActions, setCachedActions] = useLocalState(
    "ca",
    () => [] as HostedAction[]
  )
  if (props.actions.length > 0 && props.actions.length !== cachedActions.length) {
    setCachedActions(props.actions)
  }
  const actions = cachedActions.length > 0 ? cachedActions : props.actions

  const defaults = state.config || ({} as State["config"])

  const [roots, setRoots] = useLocalState("rt", () => lines(defaults.readable_roots))
  const [blocked, setBlocked] = useLocalState("bd", () => lines(defaults.blocked_dirs))
  const [exts, setExts] = useLocalState("ex", () => lines(defaults.allowed_extensions))
  const [maxFileKb, setMaxFileKb] = useLocalState("mk", () => String(defaults.max_file_kb ?? 512))
  const [maxMatches, setMaxMatches] = useLocalState("mm", () => String(defaults.max_matches ?? 50))
  const [maxLines, setMaxLines] = useLocalState("ml", () => String(defaults.max_context_lines ?? 400))
  const [followLinks, setFollowLinks] = useLocalState("fl", () => !!defaults.follow_symlinks)

  const [enableWrite, setEnableWrite] = useLocalState("ew", () => !!defaults.enable_write)
  const [allowCreate, setAllowCreate] = useLocalState("ac", () => !!defaults.allow_create)
  const [allowModify, setAllowModify] = useLocalState("am", () => !!defaults.allow_modify)
  const [allowDelete, setAllowDelete] = useLocalState("ad", () => !!defaults.allow_delete)

  const [redact, setRedact] = useLocalState("rd", () => defaults.redact_sensitive !== false)
  const [patterns, setPatterns] = useLocalState("pt", () => lines(defaults.custom_redact_patterns))

  const [indexEnabled, setIndexEnabled] = useLocalState("ie", () => defaults.index_enabled !== false)
  const [indexLimit, setIndexLimit] = useLocalState("il", () => String(defaults.index_limit ?? 20000))
  const [undoSteps, setUndoSteps] = useLocalState("us", () => String(defaults.max_undo_steps ?? 20))
  const [style, setStyle] = useLocalState("sy", () => defaults.reply_style || "gentle")

  const saveAction = actions.find((a: HostedAction) => a.id === "update_settings")
  const scanAction = actions.find((a: HostedAction) => a.id === "rebuild_index")
  const refreshAction = actions.find((a: HostedAction) => a.id === "refresh_status")

  function buildConfig(): Record<string, unknown> {
    return {
      readable_roots: toList(roots),
      blocked_dirs: toList(blocked),
      allowed_extensions: toList(exts),
      max_file_kb: Number(maxFileKb) || 512,
      max_matches: Number(maxMatches) || 50,
      max_context_lines: Number(maxLines) || 400,
      follow_symlinks: followLinks,
      enable_write: enableWrite,
      allow_create: allowCreate,
      allow_modify: allowModify,
      allow_delete: allowDelete,
      redact_sensitive: redact,
      custom_redact_patterns: toList(patterns),
      index_enabled: indexEnabled,
      index_limit: Number(indexLimit) || 20000,
      max_undo_steps: Number(undoSteps) || 20,
      reply_style: style,
    }
  }

  const status = state.status || ({} as State["status"])

  return (
    <Page title="猫娘代码协助">
      <Stack>
        <Stack>
          <Text color="secondary">
            已授权 {status.root_count ?? 0} 个目录，索引 {status.index_files ?? 0} 个文件（{fmtSize(status.index_bytes ?? 0)}）
          </Text>
          <Text color="muted">最近一次索引：{fmtTime(status.last_scan_at ?? 0)} · 后端 {status.index_backend || "live"}</Text>
          <Stack>
            <StatusBadge variant={status.write_enabled ? "warning" : "success"} label={status.write_enabled ? "写权限：已开启" : "写权限：只读"} />
            <StatusBadge variant="info" label={redact ? "脱敏：已开启" : "脱敏：已关闭"} />
            <StatusBadge variant={status.scanning ? "info" : "success"} label={status.scanning ? "索引：重建中" : "索引：空闲"} />
          </Stack>
        </Stack>

        {status.root_count === 0 && (
          <Alert variant="warning" title="还没有可读文件夹">
            <Text color="primary">先在下面填入至少一个绝对路径（例如 D:\\projects\\myapp），保存后猫娘才能读代码。</Text>
          </Alert>
        )}

        <Card title="可读文件夹">
          <Stack>
            <Field label="允许的目录（每行一个绝对路径）">
              <Textarea
                value={roots}
                onChange={(v: string) => setRoots(v)}
              />
            </Field>
            <Text color="muted">只有这些目录里的文件会被读取；越界的路径、伪装成路径的符号链接都会被拒绝。</Text>
            <Field label="遍历时跳过的目录名">
              <Textarea value={blocked} onChange={(v: string) => setBlocked(v)} />
            </Field>
            <Field label="允许的文件扩展名">
              <Textarea value={exts} onChange={(v: string) => setExts(v)} />
            </Field>
            <Field label="是否跟随符号链接（不建议开启）">
              <Switch checked={followLinks} onChange={(v: boolean) => setFollowLinks(v)} />
            </Field>
          </Stack>
        </Card>

        <Card title="隐私脱敏">
          <Stack>
            <Field label="返回给模型前先本地打码">
              <Switch checked={redact} onChange={(v: boolean) => setRedact(v)} />
            </Field>
            <Text color="muted">
              密钥、API Token、私钥块、邮箱、手机号、身份证号会被替换成占位符，明文不会离开本机。
            </Text>
            <Field label="追加自定义脱敏正则（每行一个）">
              <Textarea value={patterns} onChange={(v: string) => setPatterns(v)} />
            </Field>
          </Stack>
          <Tip>正则脱敏能挡住常见写法，但拦不住刻意拼接的凭据。真正的边界是目录白名单 + 插件本身不联网。</Tip>
        </Card>

        <Card title="写权限（默认关闭）">
          <Stack>
            <Field label="允许猫娘改动你的代码">
              <Switch checked={enableWrite} onChange={(v: boolean) => setEnableWrite(v)} />
            </Field>
            {enableWrite && (
              <Stack>
                <Divider />
                <Text color="secondary">开启后，AI 依然要先出 diff 预览、经你同意才会落盘。</Text>
                <Field label="允许修改已有文件">
                  <Switch checked={allowModify} onChange={(v: boolean) => setAllowModify(v)} />
                </Field>
                <Field label="允许新建文件">
                  <Switch checked={allowCreate} onChange={(v: boolean) => setAllowCreate(v)} />
                </Field>
                <Field label="允许删除文件（当前版本不提供删除入口）">
                  <Switch checked={allowDelete} onChange={(v: boolean) => setAllowDelete(v)} />
                </Field>
              </Stack>
            )}
          </Stack>
        </Card>

        <Card title="读取上限与索引">
          <Stack>
            <Field label="单文件读取上限（KB，32–8192）">
              <Input type="number" value={Number(maxFileKb)} min={32} max={8192} onChange={(v: number) => setMaxFileKb(String(v))} />
            </Field>
            <Field label="单次搜索最多返回条数（1–500）">
              <Input type="number" value={Number(maxMatches)} min={1} max={500} onChange={(v: number) => setMaxMatches(String(v))} />
            </Field>
            <Field label="单次返回最大行数（20–5000）">
              <Input type="number" value={Number(maxLines)} min={20} max={5000} onChange={(v: number) => setMaxLines(String(v))} />
            </Field>
            <Field label="启用本地索引（加速搜索）">
              <Switch checked={indexEnabled} onChange={(v: boolean) => setIndexEnabled(v)} />
            </Field>
            <Field label="索引文件上限（100–500000）">
              <Input type="number" value={Number(indexLimit)} min={100} max={500000} onChange={(v: number) => setIndexLimit(String(v))} />
            </Field>
            <Field label="可撤销步数（1–200）">
              <Input type="number" value={Number(undoSteps)} min={1} max={200} onChange={(v: number) => setUndoSteps(String(v))} />
            </Field>
          </Stack>
        </Card>

        <Card title="语气">
          <Stack>
            <Field label="回复风格">
              <Select
                value={style}
                onChange={(v: string) => setStyle(v)}
                options={[
                  { value: "gentle", label: "温和陪伴（推荐）" },
                  { value: "professional", label: "只讲事实" },
                  { value: "tsundere", label: "傲娇" },
                ]}
              />
            </Field>
            <Text color="muted">只影响猫娘提示语，不影响任何功能逻辑。</Text>
          </Stack>
        </Card>

        <Section>
          <Stack>
            {saveAction ? (
              <ActionButton action={saveAction} values={{ config: buildConfig() }}>保存设置</ActionButton>
            ) : (
              <ActionButton action={{ id: "update_settings", call: async () => {} } as HostedAction} values={{ config: buildConfig() }}>
                保存设置（加载中…）
              </ActionButton>
            )}
            {scanAction ? (
              <ActionButton action={scanAction} values={{}}>重建索引</ActionButton>
            ) : null}
            {refreshAction ? (
              <ActionButton action={refreshAction} values={{}}>刷新状态</ActionButton>
            ) : null}
          </Stack>
          <Stack>
            <StatCard title="索引文件" value={String(status.index_files ?? 0)} />
            <StatCard title="可撤销" value={String(status.undo_depth ?? 0)} />
            <StatCard title="项目笔记" value={String(status.note_count ?? 0)} />
          </Stack>
        </Section>
      </Stack>
    </Page>
  )
}
