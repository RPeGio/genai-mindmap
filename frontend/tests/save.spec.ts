import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MapRecord, MindMapContent, MindNode } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

const tree = (): MindNode => ({
  id: 'root',
  label: '根',
  children: [{ id: 'a', label: '甲', children: [] }],
});

const sample = (): MindMapContent => ({ title: '示例', root: tree() });

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

interface Call {
  url: string;
  method: string;
  body: unknown;
}

let calls: Call[] = [];

function stubFetch(handler: (call: Call) => Response) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const call: Call = {
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      return handler(call);
    }),
  );
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('保存', () => {
  it('第一次保存走 POST，存完记住 id 与版本号', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '甲改');
    expect(store.dirty).toBe(true);

    stubFetch(() =>
      jsonResponse(
        { id: 'm1', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' } satisfies MapRecord,
        201,
      ),
    );

    await expect(store.save()).resolves.toBe('saved');
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/mindmaps');
    expect(store.mapId).toBe('m1');
    expect(store.version).toBe(1);
    expect(store.dirty).toBe(false);
  });

  it('再次保存走 PUT，并把读到的版本号带回去', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '甲改');

    stubFetch(call =>
      jsonResponse({
        id: 'm1',
        version: call.method === 'POST' ? 1 : 2,
        content: sample(),
        updated_at: '2026-11-04T00:00:00Z',
      } satisfies MapRecord),
    );

    await store.save();
    store.commitLabel('a', '再改');
    await store.save();

    expect(calls.map(call => call.method)).toEqual(['POST', 'PUT']);
    expect(calls[1].url).toBe('/api/mindmaps/m1');
    expect(calls[1].body).toMatchObject({ version: 1 });
    expect(store.version).toBe(2);
  });

  it('409 时保留本地修改，并把冲突交给界面处理', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '甲改');

    stubFetch(() => jsonResponse({ detail: '导图已被修改，请重新打开后再保存' }, 409));

    await expect(store.save()).resolves.toBe('conflict');
    expect(store.conflict).not.toBeNull();
    expect(store.conflict?.message).toContain('导图已被修改');
    expect(store.dirty).toBe(true);
  });

  it('冲突时另存为新导图：走 POST，并清掉冲突状态', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    // 先当成"已保存过的导图"，这样保存才会走 PUT（也才可能撞上 409）。
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', '甲改');

    let postCount = 0;
    stubFetch(call => {
      if (call.method === 'PUT') return jsonResponse({ detail: '导图已被修改' }, 409);
      postCount += 1;
      return jsonResponse({ id: 'm2', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' } satisfies MapRecord, 201);
    });

    await store.save();
    await store.saveAsNew();

    expect(postCount).toBe(1);
    expect(store.conflict).toBeNull();
    expect(store.mapId).toBe('m2');
    expect(store.dirty).toBe(false);
  });

  it('冲突时加载服务端版本：内容被替换、撤销栈清空', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '本地改动');

    stubFetch(call => {
      if (call.method === 'PUT') return jsonResponse({ detail: '导图已被修改' }, 409);
      return jsonResponse({
        id: 'm1',
        version: 5,
        content: { title: '服务端标题', root: tree() },
        updated_at: '2026-11-04T02:00:00Z',
      } satisfies MapRecord);
    });

    await store.save();
    await store.reloadFromServer();

    expect(store.conflict).toBeNull();
    expect(store.content?.title).toBe('服务端标题');
    expect(store.version).toBe(5);
    expect(store.canUndo).toBe(false);
    expect(store.dirty).toBe(false);
  });

  it('内容不合法时不发请求', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    // 直接放一份空标题的树：界面上的编辑入口会拦，但保存这一层必须自己再兜一次。
    store.content = { title: '示例', root: { id: 'r', label: '  ', children: [] } };

    stubFetch(() => jsonResponse({}, 500));
    await expect(store.save()).resolves.toBe('invalid');
    expect(calls).toHaveLength(0);
    expect(store.status).toBe('节点标题不能为空');
  });
});

describe('打开列表', () => {
  it('拉列表失败时给出提示，不留下空白的成功假象', async () => {
    const store = useMindMapStore();
    stubFetch(() => jsonResponse({ detail: '请求的地址不存在' }, 404));

    await store.openList();

    expect(store.maps).toEqual([]);
    expect(store.listOpen).toBe(true);
    expect(store.status).toBe('请求的地址不存在');
  });

  it('列表里的导图可以打开，打开后不脏', async () => {
    const store = useMindMapStore();
    stubFetch(() =>
      jsonResponse({
        id: 'm9',
        version: 2,
        content: { title: '打开这张', root: tree() },
        updated_at: '2026-11-04T03:00:00Z',
      } satisfies MapRecord),
    );

    await store.openMap('m9');

    expect(store.content?.title).toBe('打开这张');
    expect(store.mapId).toBe('m9');
    expect(store.version).toBe(2);
    expect(store.dirty).toBe(false);
    expect(store.listOpen).toBe(false);
  });
});
