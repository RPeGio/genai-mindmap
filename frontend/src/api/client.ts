import type {
  CreateTextSourceResponse,
  GenerateResponse,
  MapRecord,
  MindMapContent,
  MindNode,
  PipelineResponse,
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

/** 同步生成：文本 → 导图候选。不落库，用户确认后再保存。 */
export function generateMindMap(text: string): Promise<GenerateResponse> {
  return request<GenerateResponse>('/generations', {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
}

// ---- 保存与打开 ----

/** 已保存的导图列表（不含筛选，按更新时间倒序由后端决定）。 */
export function listMindMaps(): Promise<MapRecord[]> {
  return request<MapRecord[]>('/mindmaps');
}

export function getMindMap(id: string): Promise<MapRecord> {
  return request<MapRecord>(`/mindmaps/${encodeURIComponent(id)}`);
}

/**
 * 发出去之前裁成后端当前认识的形状。
 *
 * 后端 v1 的 `MindMapContent` 是 `extra="forbid"`：多一个 `note` 就直接 422，
 * 而 `note` 属于契约 v2（见 `docs/HANDOFF.md` H1，与 v2 同批上线）。
 * 所以这里显式按 v1 序列化：本地留着 `note` 供界面显示正文，但不把后端还不认识的字段发出去。
 */
function toContractV1(content: MindMapContent): MindMapContent {
  // 只裁 note —— 它是契约 v2 字段，v1 后端的 Node 是 extra="forbid"，
  // 带着它发出去必然 422。
  //
  // source_refs 必须保留：它在 v1 契约里本来就有（contracts/mindmap.py 的 Node），
  // 之前这里一并裁掉，表现是保存后引用全部消失、刷新后再也读不回来。
  const strip = (node: MindNode): MindNode => ({
    id: node.id,
    label: node.label,
    children: node.children.map(strip),
    ...(node.source_refs?.length ? { source_refs: node.source_refs } : {}),
  });
  return {
    title: content.title,
    root: strip(content.root),
    ...(content.source_usage ? { source_usage: content.source_usage } : {}),
  };
}

/** 第一次保存：创建一条新记录，版本由服务端给（1）。 */
export function createMindMap(content: MindMapContent): Promise<MapRecord> {
  return request<MapRecord>('/mindmaps', {
    method: 'POST',
    body: JSON.stringify({ content: toContractV1(content) }),
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
    body: JSON.stringify({ content: toContractV1(content), version }),
  });
}

// ---- 流式生成 ----

/** 后端 generation/stream.py 推送的事件。类型放在 JSON 的 type 字段里。 */
export type StreamEvent =
  | { type: 'stage'; stage: string; progress: number; detail: string }
  | { type: 'skeleton'; content: MindMapContent }
  | { type: 'node'; parentId: string | null; node: { id: string; label: string; children: [] }; index: number }
  | { type: 'heartbeat'; waitedMs: number }
  | { type: 'done'; mode: 'demo' | 'model'; content: MindMapContent; latencyMs: number; degraded: boolean; reason?: string }
  | { type: 'error'; code: string; message: string };

/**
 * 流式生成：先收骨架再收细节，回调按到达顺序触发。
 *
 * 不用 EventSource 是因为它只能发 GET，而输入文本可能很长；这里用 fetch
 * 的 ReadableStream 自己切帧。SSE 的帧以空行分隔，每帧取第一条 `data: ` 行。
 */
export async function generateMindMapStream(
  text: string,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/generations/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal,
    });
  } catch (error) {
    if ((error as Error).name === 'AbortError') return;
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok || !response.body) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    throw new ApiError(response.status, body.code ?? `http_${response.status}`,
      errorMessage(response.status, body), body.request_id);
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
      onEvent(JSON.parse(data) as StreamEvent);
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

// ---- 文档上传与多来源生成 ----

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

// ---- 导出 ----

/** 后端三种格式都是纯文本响应，不能走 request()——它固定按 JSON 解析。 */
export type ExportFormat = 'puml' | 'markdown' | 'opml';

const EXPORT_META: Record<ExportFormat, { label: string; extension: string }> = {
  puml: { label: 'PlantUML', extension: 'puml' },
  markdown: { label: 'Markdown', extension: 'md' },
  opml: { label: 'OPML', extension: 'opml' },
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
      body: JSON.stringify({ content }),
    });
  } catch {
    throw new ApiError(0, 'network_error', '无法连接后端，请确认服务已启动。');
  }

  if (!response.ok) throw await toApiError(response);
  return response.text();
}

/** 把解析好的来源原样送回流水线生成导图。不做裁剪：后端 Source 是 extra="forbid"，少字段才安全。 */
export function generateFromSources(sources: Source[], signal?: AbortSignal): Promise<PipelineResponse> {
  return request<PipelineResponse>('/pipeline/generate', {
    method: 'POST',
    body: JSON.stringify({ sources }),
    signal,
  });
}
