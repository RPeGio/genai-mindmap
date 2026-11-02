import type { GenerateResponse } from './types';

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
