import { afterEach, describe, expect, it, vi } from 'vitest';

import { exportFilename, exportLabel, exportMindMap } from '../src/api/client';
import type { MindMapContent } from '../src/api/types';

const CONTENT: MindMapContent = {
  title: '季度规划',
  root: { id: 'root', label: '产品', children: [] },
} as MindMapContent;

describe('exportFilename', () => {
  it('用标题加对应扩展名', () => {
    expect(exportFilename('puml', '季度规划')).toBe('季度规划.puml');
    expect(exportFilename('markdown', '季度规划')).toBe('季度规划.md');
    expect(exportFilename('opml', '季度规划')).toBe('季度规划.opml');
  });

  it('标题里的路径分隔符和引号会被替换', () => {
    // 标题是模型生成的自由文本，可能带斜杠或引号，直接当文件名会失败
    expect(exportFilename('puml', 'a/b\\c:d*e?f"g<h>i|j')).toBe('a_b_c_d_e_f_g_h_i_j.puml');
  });

  it('标题为空时退回默认名', () => {
    expect(exportFilename('markdown', '   ')).toBe('导图.md');
  });

  it('去掉换行', () => {
    expect(exportFilename('opml', '第一行\n第二行')).toBe('第一行_第二行.opml');
  });
});

describe('exportLabel', () => {
  it('三种格式都有可读名称', () => {
    expect(exportLabel('puml')).toBe('PlantUML');
    expect(exportLabel('markdown')).toBe('Markdown');
    expect(exportLabel('opml')).toBe('OPML');
  });
});

describe('exportMindMap', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('返回纯文本而不是 JSON', async () => {
    // 后端三种导出都是 text/*，走 request() 会因 response.json() 失败
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => '@startmindmap\n* 产品\n@endmindmap\n',
    });
    vi.stubGlobal('fetch', fetchMock);

    const text = await exportMindMap('puml', CONTENT);

    expect(text).toContain('@startmindmap');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/export/puml');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ content: CONTENT });
    vi.unstubAllGlobals();
  });


  it('失败时抛出带后端文案的错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ detail: '内容不合法' }),
    }));

    await expect(exportMindMap('opml', CONTENT)).rejects.toThrow('内容不合法');
    vi.unstubAllGlobals();
  });

  it('连不上后端时提示检查服务', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    await expect(exportMindMap('markdown', CONTENT)).rejects.toThrow(/无法连接后端/);
    vi.unstubAllGlobals();
  });

  it('把 note 折进 label 再发，字段本身不带 v2 的 note', async () => {
    // 样例导图里的节点带 note（正文）：v1 的 Node 不认这个字段，直接发会 422；
    // 但也不能一裁了之——导出的成品只剩短标题就看不懂了。
    const withNote = {
      title: '季度规划',
      root: { id: 'root', label: '产品', note: '正文放在 note 里', children: [] },
    } as MindMapContent;
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, text: async () => '' });
    vi.stubGlobal('fetch', fetchMock);

    await exportMindMap('markdown', withNote);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const sent = JSON.parse(String(init.body)) as { content: { root: Record<string, unknown> } };
    expect(sent.content.root).not.toHaveProperty('note');
    // note 自己就以标题开头时不再重复拼一遍（实际数据里很常见）
    expect(sent.content.root.label).toBe('产品：正文放在 note 里');
  });

  it('正文已经以标题开头时不再重复标题', async () => {
    const content = {
      title: '机器学习',
      root: {
        id: 'root',
        label: '分类',
        note: '分类：输出离散标签。',
        children: [],
      },
    } as MindMapContent;
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, text: async () => '' });
    vi.stubGlobal('fetch', fetchMock);

    await exportMindMap('markdown', content);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const sent = JSON.parse(String(init.body)) as { content: { root: Record<string, unknown> } };
    expect(sent.content.root.label).toBe('分类：输出离散标签。');
    vi.unstubAllGlobals();
  });
});
