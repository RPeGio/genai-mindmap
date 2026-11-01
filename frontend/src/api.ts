import type { Generation, MindMapContent, MindMapRecord } from './types';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}
export const api = {
  generate: (text: string) => request<Generation>('/generations', { method: 'POST', body: JSON.stringify({ text }) }),
  list: () => request<MindMapRecord[]>('/mindmaps'),
  get: (id: string) => request<MindMapRecord>(`/mindmaps/${encodeURIComponent(id)}`),
  create: (content: MindMapContent) => request<MindMapRecord>('/mindmaps', { method: 'POST', body: JSON.stringify({ content }) }),
  update: (record: MindMapRecord, content: MindMapContent) => request<MindMapRecord>(`/mindmaps/${encodeURIComponent(record.id)}`, { method: 'PUT', body: JSON.stringify({ version: record.version, content }) }),
};
