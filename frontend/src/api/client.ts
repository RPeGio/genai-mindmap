import type { GenerateResponse, MapRecord, MindMapContent, MindNode } from './types';

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

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    throw new ApiError(response.status, body.code ?? `http_${response.status}`, errorMessage(response.status, body), body.request_id);
  }
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
  const strip = (node: MindNode): MindNode => ({
    id: node.id,
    label: node.label,
    children: node.children.map(strip),
  });
  return { title: content.title, root: strip(content.root) };
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
