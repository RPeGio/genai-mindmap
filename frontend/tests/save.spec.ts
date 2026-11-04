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

/** 构造一个立刻给出 done 帧再 [DONE] 的 SSE 响应，用来跑通流式生成。 */
function streamWithDone(content: MindMapContent): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const frame = (payload: unknown) =>
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
      frame({ type: 'done', mode: 'model', content, latencyMs: 100, degraded: false, reason: '' });
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
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

  it('保存时把 v2 的 note 裁掉，只发 v1 认识的字段', async () => {
    const store = useMindMapStore();
    // v1 后端是 extra="forbid"：带上 note 会直接 422（见 docs/HANDOFF.md H1）。
    store.preview(
      {
        title: '示例',
        root: {
          id: 'root',
          label: '根',
          note: '这是只在界面上显示的正文',
          children: [{ id: 'a', label: '甲', note: '子节点的正文', children: [] }],
        },
      },
      'right',
    );

    stubFetch(() =>
      jsonResponse({ id: 'm1', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' }, 201),
    );

    await expect(store.save()).resolves.toBe('saved');
    const body = calls[0].body as { content: { root: MindNode } };
    expect(JSON.stringify(body)).not.toContain('note');
    expect(body.content.root.children[0].label).toBe('甲');
    // 本地仍保留 note，详情面板要能显示全文。
    expect(store.content?.root.note).toBe('这是只在界面上显示的正文');
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

  it('保存期间继续编辑：请求返回后仍标记为有未保存修改', async () => {
    // 请求体里发的是点保存那一刻的树；在途时的改动没有被发出去。
    // markSaved() 会无条件把 dirty 清掉，如果这里不恢复，界面会显示"已保存"，
    // 用户以为后改的内容也存上了，刷新即丢。
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '甲改');

    stubFetch(() => {
      // 模拟请求在途时用户又改了内容
      store.commitLabel('a', '甲又改');
      return jsonResponse(
        { id: 'm1', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' },
        201,
      );
    });

    await expect(store.save()).resolves.toBe('saved');

    // 版本号确实存上了，但本地仍有未保存的修改
    expect(store.version).toBe(1);
    expect(store.mapId).toBe('m1');
    expect(store.dirty).toBe(true);
    expect(store.status).toContain('保存期间又有修改');
    // 改动的内容没有被服务端返回的旧内容覆盖掉
    expect(store.content?.root.children[0].label).toBe('甲又改');
  });

  it('保存期间没有改动时，正常清掉脏标记', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a', '甲改');

    stubFetch(() =>
      jsonResponse(
        { id: 'm1', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' },
        201,
      ),
    );

    await expect(store.save()).resolves.toBe('saved');
    expect(store.dirty).toBe(false);
    expect(store.status).toBe('已保存，版本 1。');
  });

  it('另存为新导图期间继续编辑，同样保留脏标记', async () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    stubFetch(() => {
      store.commitLabel('a', '甲又改');
      return jsonResponse(
        { id: 'm2', version: 1, content: sample(), updated_at: '2026-11-04T00:00:00Z' },
        201,
      );
    });

    await store.saveAsNew();
    expect(store.mapId).toBe('m2');
    expect(store.conflict).toBeNull();
    expect(store.dirty).toBe(true);
    expect(store.status).toContain('保存期间又有修改');
  });

  it('保存期间切到另一张导图：响应不会把身份改回上一张', async () => {
    // 危险场景：A 的保存请求在途时打开 B，响应回来后若把 mapId 改回 A，
    // 画布显示的是 B、身份却是 A，再点保存就会把 B 的内容写进 A。
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 的改动');

    const other: MindMapContent = {
      title: 'B 导图',
      root: { id: 'root', label: 'B 根', children: [] },
    };

    stubFetch(() => {
      // 模拟请求在途时用户打开了另一张导图
      store.loadMap(other, { id: 'm2', version: 3, updated_at: '2026-11-04T05:00:00Z' });
      return jsonResponse({
        id: 'm1',
        version: 2,
        content: sample(),
        updated_at: '2026-11-04T01:00:00Z',
      } satisfies MapRecord);
    });

    await expect(store.save()).resolves.toBe('saved');

    // 身份与内容都还是 B，没有被 A 的响应覆盖
    expect(store.mapId).toBe('m2');
    expect(store.version).toBe(3);
    expect(store.content?.title).toBe('B 导图');
    expect(store.status).toContain('当前导图未受影响');
  });

  it('切图后再保存，PUT 打到新导图的 id 上', async () => {
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 的改动');

    const other: MindMapContent = {
      title: 'B 导图',
      root: { id: 'root', label: 'B 根', children: [] },
    };

    let phase = 0;
    stubFetch(() => {
      phase += 1;
      if (phase === 1) {
        store.loadMap(other, { id: 'm2', version: 3, updated_at: '2026-11-04T05:00:00Z' });
        return jsonResponse({
          id: 'm1', version: 2, content: sample(), updated_at: '2026-11-04T01:00:00Z',
        } satisfies MapRecord);
      }
      return jsonResponse({
        id: 'm2', version: 4, content: other, updated_at: '2026-11-04T06:00:00Z',
      } satisfies MapRecord);
    });

    await store.save();                 // 保存 A（期间切到 B）
    store.renameTitle('B 导图改名');     // 在 B 上改点东西
    await store.save();                 // 保存 B

    expect(calls).toHaveLength(2);
    expect(calls[0].url).toBe('/api/mindmaps/m1');
    expect(calls[1].url).toBe('/api/mindmaps/m2');
    expect(calls[1].method).toBe('PUT');
  });

  it('保存期间新建导图：响应同样不改身份', async () => {
    // 边界：保存前 mapId 是 m1，新建后是 null，直接比 mapId 判断不出"切走过"，
    // 所以用会话号。这里断言新建的空白图不会被挂上 m1 的身份。
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 的改动');

    stubFetch(() => {
      store.newMap();
      return jsonResponse({
        id: 'm1', version: 2, content: sample(), updated_at: '2026-11-04T01:00:00Z',
      } satisfies MapRecord);
    });

    await store.save();

    expect(store.mapId).toBeNull();
    expect(store.content).toBeNull();
    expect(store.version).toBe(0);
  });

  it('切图后新页面的保存状态不会被上一轮 finally 清掉', async () => {
    // A 的 finally 若无条件把 saving 置 false，会把 B 正在保存的状态也清掉，
    // 用户连点两次就会发出两个请求。
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 改');

    const other: MindMapContent = {
      title: 'B 导图',
      root: { id: 'root', label: 'B 根', children: [] },
    };

    stubFetch(() => {
      store.loadMap(other, { id: 'm2', version: 3, updated_at: '2026-11-04T05:00:00Z' });
      return jsonResponse({
        id: 'm1', version: 2, content: sample(), updated_at: '2026-11-04T01:00:00Z',
      } satisfies MapRecord);
    });

    await store.save();
    // 切图后 saving 已被 loadMap 重置，新页面可以立即保存
    expect(store.saving).toBe(false);
  });
  it('保存期间重新生成：旧响应不会把身份套到生成出的新图上', async () => {
    // 文本生成同样会换掉整张图，会话必须跟着切。否则 A 的保存响应回来时
    // 会话号没变，mapId 会被写回 A，之后保存新图就 PUT 到 A 的记录上。
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 的改动');

    let releaseSave: (() => void) | null = null;
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      const url = String(input);
      if (url.includes('/generations/stream')) {
        return streamWithDone({
          title: '新生成的图',
          root: { id: 'nr', label: '新根', children: [] },
        });
      }
      await new Promise<void>(resolve => { releaseSave = resolve; });
      return jsonResponse({
        id: 'm1', version: 2, content: sample(), updated_at: '2026-11-04T01:00:00Z',
      } satisfies MapRecord);
    }));

    const saving = store.save();
    await vi.waitFor(() => expect(releaseSave).not.toBeNull());

    // 保存还没回来，用户重新生成了一张新图
    await store.generate('新主题');

    releaseSave!();
    await saving;

    expect(store.mapId).toBeNull();
    expect(store.version).toBe(0);
    expect(store.content?.title).toBe('新生成的图');
  });

  it('保存期间切换到新生成的导图：旧响应不会把身份套上来', async () => {
    // preview() 是"换了一张图"，必须递增会话号。否则 A 的保存响应回来时
    // 会话号仍是旧的，mapId 会被写成 A，而画布上是新图——下次保存就把
    // 新内容 PUT 到 A 的记录上。
    const store = useMindMapStore();
    store.loadMap(sample(), { id: 'm1', version: 1, updated_at: '2026-11-04T00:00:00Z' });
    store.commitLabel('a', 'A 的改动');

    let release: (() => void) | null = null;
    stubFetch(async () => {
      await new Promise<void>(resolve => { release = resolve; });
      return jsonResponse({
        id: 'm1', version: 2, content: sample(), updated_at: '2026-11-04T01:00:00Z',
      } satisfies MapRecord);
    });

    const saving = store.save();
    await vi.waitFor(() => expect(release).not.toBeNull());

    // 保存还没回来，上传生成出了一张新图
    store.preview(
      { title: '生成的新图', root: { id: 'nr', label: '新根', children: [] } },
      'right',
    );

    release!();
    await saving;

    // 新图不该挂上 m1 的身份
    expect(store.mapId).toBeNull();
    expect(store.version).toBe(0);
    expect(store.content?.title).toBe('生成的新图');
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
