import type {
  AgentEvent,
  AgentRunRequest,
  CreateTextSourceResponse,
  ImportResult,
  MapRecord,
  MindMapContent,
  Pref,
  PrefSuggestion,
  RunDetail,
  RunSummary,
  SavedPref,
  Source,
} from './types';

/** 后端错误体现在只返回 detail；契约 v2 会带 code/request_id，这里先兼容两种。 */
interface ErrorBody {
  code?: string;
  message?: string;
  detail?: unknown;
  request_id?: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

const BASE = import.meta.env.VITE_API_BASE ?? '/api';

function errorMessage(status: number, body: ErrorBody): string {
  if (typeof body.message === 'string' && body.message) return body.message;
  if (typeof body.detail === 'string' && body.detail) return body.detail;
  return `请求失败（HTTP ${status}）`;
}

/**
 * 后端错误体 → ApiError。JSON 解析失败时退化成空对象，保底给出 HTTP 状态码。
 * JSON 请求与 multipart 上传共用这一处，免得两边文案各自漂移。
 */
async function toApiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => ({}))) as ErrorBody;
  return new ApiError(
    response.status,
    body.code ?? `http_${response.status}`,
    errorMessage(response.status, body),
    body.request_id,
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---- 保存与打开 ----

/** 已保存的导图列表（不含筛选，按更新时间倒序由后端决定）。 */
export function listMindMaps(): Promise<MapRecord[]> {
  return request<MapRecord[]>('/mindmaps');
}

export function getMindMap(id: string): Promise<MapRecord> {
  return request<MapRecord>(`/mindmaps/${encodeURIComponent(id)}`);
}

/** 第一次保存：创建一条新记录，版本由服务端给（1）。 */
export function createMindMap(content: MindMapContent): Promise<MapRecord> {
  return request<MapRecord>('/mindmaps', {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}

/**
 * 覆盖保存已有导图。`version` 是"我打开时看到的版本"：
 * 服务端发现不是最新就返回 409，不会用旧内容盖掉别人的修改。
 */
export function updateMindMap(
  id: string,
  content: MindMapContent,
  version: number,
): Promise<MapRecord> {
  return request<MapRecord>(`/mindmaps/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify({ content, version }),
  });
}

// ---- agent 流式运行 ----

/**
 * 通用 SSE 读取：POST 一个 JSON，按帧回调。
 *
 * 不用 EventSource 是因为它只能发 GET，而输入文本可能很长。这里用 fetch 的
 * ReadableStream 自己切帧：SSE 的帧以空行分隔，每帧取第一条 `data: ` 行。
 *
 * 单独抽出来是为了让帧解析只有一份：`[DONE]` 终止帧、分片边界、单帧解析失败
 * 跳过，这些细节各写一遍就会各自漂移，出现"同样的网络条件下只有一条流偶发丢帧"。
 */
async function postSse<T>(
  url: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if ((error as Error).name === 'AbortError') return;
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok || !response.body) {
    const errorBody = (await response.json().catch(() => ({}))) as ErrorBody;
    throw new ApiError(response.status, errorBody.code ?? `http_${response.status}`,
      errorMessage(response.status, errorBody), errorBody.request_id);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const flush = (frame: string) => {
    const line = frame.split('\n').find(part => part.startsWith('data: '));
    if (!line) return;
    const data = line.slice(6).trim();
    // 终止帧沿用 OpenAI 的 [DONE] 约定，不是 JSON，遇到即表示流正常结束。
    if (data === '[DONE]') return;
    try {
      onEvent(JSON.parse(data) as T);
    } catch {
      // 单帧解析失败不该打断整条流，跳过即可。
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        flush(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        cut = buffer.indexOf('\n\n');
      }
    }
    if (buffer.trim()) flush(buffer);
  } catch (error) {
    // 主动取消（点「取消」）不是错误；其余中断要让调用方知道流没走完，
    // 否则界面会停在「正在生成」上，看起来像卡死。
    if ((error as Error).name === 'AbortError') return;
    throw new ApiError(0, 'stream_interrupted', '连接中断，请重试。');
  }
}

export function runAgentStream(
  body: AgentRunRequest,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return postSse<AgentEvent>(`${BASE}/agent/runs/stream`, body, onEvent, signal);
}

// ---- 运行台账（run 的身份与草稿）----

/**
 * 上次跑完、但用户还没处理过的运行。
 *
 * 默认取后端给的"有草稿且未应用/未丢弃"那一批，其中**包含"被取消但已经产出草稿"
 * 的运行**：那正是"关掉页面那 40 秒别白花"要捡回来的东西。只查 `done` 的话，
 * 草稿会被写进库、然后再也没人读过——表就成了一行字。
 */
export function listRuns(status?: RunSummary['status']): Promise<RunSummary[]> {
  const query = new URLSearchParams({ unapplied: 'true' });
  if (status) query.set('status', status);
  return request<RunSummary[]>(`/agent/runs?${query.toString()}`);
}

/** 单条详情，带候选正文。只有点「查看」时才值得走这一趟。 */
export function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/agent/runs/${encodeURIComponent(runId)}`);
}

/**
 * 候选进了画布 / 被用户丢弃。两者分开记：`applied_at` 是"采纳过"，`dismissed_at`
 * 是"嫌弃过"，下次进页面该不该再问是由它们区分的。
 *
 * 幂等，且只在"本来为空"时打第一个时间戳，所以前端可以放心重试。
 */
export function markRunResolved(
  runId: string,
  resolution: 'applied' | 'dismissed',
): Promise<void> {
  return request<void>(`/agent/runs/${encodeURIComponent(runId)}/${resolution}`, {
    method: 'POST',
  });
}

// ---- 文档上传 ----

/**
 * 上传文档，由后端解析成文本来源。
 *
 * 不走 request()：它固定带 `Content-Type: application/json`，而 multipart 的
 * boundary 必须由浏览器自己生成。手动设头的结果是后端拆不出 file 字段，
 * 所以这里只给 method 和 body，让 fetch 自己填类型。
 */
export async function uploadDocument(file: File, signal?: AbortSignal): Promise<Source> {
  const form = new FormData();
  form.append('file', file);

  let response: Response;
  try {
    response = await fetch(`${BASE}/ingestion/files`, { method: 'POST', body: form, signal });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);

  const body = (await response.json().catch(() => null)) as CreateTextSourceResponse | null;
  if (!body?.source?.id) {
    // 契约里解析失败一定走 422；拿到空壳说明后端出了问题，别把 undefined 传给下一步。
    throw new ApiError(response.status, 'empty_source', '后端没有返回解析结果，请重试。');
  }
  return body.source;
}

// ---- 用户偏好 ----

/**
 * 全部偏好，带每条的"上次生效"。
 *
 * 启动时调一次，结果填进 `usePrefs` 与 `useGenerationOptions`：偏好面板要列它，
 * 生成面板的下拉要拿它当预填值。**两处读同一份**——各拉一次会先制造"面板改完、
 * 下拉没变"的空窗。
 */
export function listPrefs(): Promise<Pref[]> {
  return request<Pref[]>('/prefs');
}

/**
 * 写一条。`changed=false` 表示这次提交没有改变任何东西（同值）。
 *
 * `source` 只有用户在建议里点"设为默认"时才传 `suggestion_accepted`：两种来源在
 * 面板上显示不同的标签，而那个区别只有写入的这一刻知道。
 */
export function setPref(
  key: string,
  value: string,
  source: 'user_explicit' | 'suggestion_accepted' = 'user_explicit',
): Promise<SavedPref> {
  return request<SavedPref>(`/prefs/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify({ value, source }),
  });
}

/** 删一条。**硬删**：没有软删，也就不存在"删了但还能翻出来"这种界面谎言。 */
export function deletePref(key: string): Promise<void> {
  return request<void>(`/prefs/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

/**
 * 候选建议，**只读不落库**。
 *
 * 用户点"设为默认"才会走 `setPref`；点了"不用"则什么都不发生——一次拒绝不需要
 * 留下任何记录。
 */
export function listPrefSuggestions(): Promise<PrefSuggestion[]> {
  return request<PrefSuggestion[]>('/prefs/suggestions');
}

// ---- 导出 ----

/**
 * 四种导出都是纯文本响应，不能走 request()——它固定按 JSON 解析。
 *
 * `json` 是**唯一无损**的一种：另外三种都只有一个文字槽位（正文优先），
 * `note` / `detail` 的区分、颜色、图标、图型、附加边往返一次就退化了。
 * 它也是导入的保底——"导出改一改再导入"只有这条路能保证一条都不丢。
 */
export type ExportFormat = 'puml' | 'markdown' | 'opml' | 'json';

const EXPORT_META: Record<ExportFormat, { label: string; extension: string }> = {
  puml: { label: 'PlantUML', extension: 'puml' },
  markdown: { label: 'Markdown', extension: 'md' },
  opml: { label: 'OPML', extension: 'opml' },
  json: { label: 'JSON', extension: 'json' },
};

export function exportLabel(format: ExportFormat): string {
  return EXPORT_META[format].label;
}

/** 文件名用导图标题，但要去掉路径分隔符等非法字符——标题是模型生成的自由文本。 */
export function exportFilename(format: ExportFormat, title: string): string {
  const safe = title.replace(/[\\/:*?"<>|\r\n]/g, '_').trim() || '导图';
  return `${safe}.${EXPORT_META[format].extension}`;
}

export async function exportMindMap(
  format: ExportFormat,
  content: MindMapContent,
): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/export/${format}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 后端四种导出统一取 `note or label`（JSON 除外，它全字段照抄），这里直接发
      // 原样内容：前端不再自己做"把正文折进标题"的拼接，避免同一份内容两端拼出两种结果。
      body: JSON.stringify({ content }),
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);
  return response.text();
}

// ---- 导入 ----

/**
 * 上传一个已有的思维导图文件，由后端识别格式并解析成 `MindMapContent`。
 *
 * 与 `uploadDocument` 同一个形状（multipart 不能走 `request()`：它固定带
 * `Content-Type: application/json`，而 multipart 的 boundary 必须由浏览器生成）。
 *
 * **后端只解析不落库**：拿到 `content` 之后由 store 装进画布，形态是"有未保存
 * 修改的新导图"，用户点保存才写库。这样导入失败不会污染已有数据。
 */
export async function importMindMap(
  file: File,
  signal?: AbortSignal,
): Promise<ImportResult> {
  const form = new FormData();
  // 字段名必须是 `file`：后端签名是 `file: UploadFile = File(...)`。
  form.append('file', file);

  let response: Response;
  try {
    response = await fetch(`${BASE}/mindmaps/import`, { method: 'POST', body: form, signal });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);

  const body = (await response.json().catch(() => null)) as ImportResult | null;
  if (!body?.content?.root) {
    // 契约里失败一定走 400；拿到空壳说明后端出了问题，别把 undefined 塞进画布。
    throw new ApiError(response.status, 'empty_import', '后端没有返回解析结果，请重试。');
  }
  return body;
}
