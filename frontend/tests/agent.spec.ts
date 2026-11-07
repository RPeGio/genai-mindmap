import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import SourceInputs from '../src/components/SourceInputs.vue';
import type { MindMapContent, Source } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/** 把若干事件包成 SSE 帧，模拟后端 /api/agent/runs/stream。 */
function agentReply(frames: unknown[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      }
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

/** 记录每次 fetch 的 URL 与 body，方便断言请求形态。 */
function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

/** 一次性 JSON 响应：openMap / save 走 request()，只读 json，不读流。 */
function jsonReply(payload: unknown): Response {
  return { ok: true, status: 200, body: null, json: async () => payload } as unknown as Response;
}

/** 已保存导图的记录，字段与 MapRecord 一致。 */
function mapRecord(id: string, payload: MindMapContent) {
  return { id, version: 3, content: payload, updated_at: '2024-01-01T00:00:00Z' };
}

/** 先把一张有 id 的导图打开，供"接受候选之后身份怎么变"的用例起手。 */
async function openExisting(id: string) {
  vi.stubGlobal('fetch', vi.fn(async () => jsonReply(mapRecord(id, content('原图')))));
  const store = useMindMapStore();
  await store.openMap(id);
  return store;
}

/**
 * 手动控制节奏的 SSE 响应：状态行这些"只在流进行中才有值"的字段，
 * 用一次性返回的流测不到——流一结束收尾就把它们清空了。
 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = (frame) => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
  // agent 路径要开着面板才渲染，用例会临时改环境变量，别让它漏到下一个文件。
  vi.unstubAllEnvs();
});

describe('agent 运行', () => {
  it('create 意图不带 snapshot，请求打到 agent 路由', async () => {
    const calls = stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      doneFrame(content()),
    ]));
    const store = useMindMapStore();

    await store.runGeneration('给机器学习做一张导图', 'create');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/agent/runs/stream');
    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('create');
    // create 带 snapshot 会被后端 422 拒，前端不能顺手传一个
    expect(body.snapshot ?? null).toBeNull();
  });

  it('edit 意图带上当前树', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content('改后'))]));
    const store = useMindMapStore();
    store.preview(content(), 'right');

    await store.runGeneration('把根改名', 'edit');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect((body.snapshot as MindMapContent).root.label).toBe('根');
  });

  it('done 只产生候选，不动画布', async () => {
    stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      { type: 'tool_start', seq: 2, call_id: 'c1', name: 'mindmap_generate' },
      { type: 'tool_end', seq: 3, call_id: 'c1', name: 'mindmap_generate', ok: true, latency_ms: 12 },
      doneFrame(content('新图')),
    ]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    await store.runGeneration('做一张导图', 'create');

    expect(store.candidate?.root.label).toBe('新图');
    // 关键约束：候选不自动应用，画布仍是旧内容
    expect(store.content?.root.label).toBe('旧图');
    expect(store.dirty).toBe(false);
    expect(store.agentEvents.map(event => event.type))
      .toEqual(['stage', 'tool_start', 'tool_end', 'done']);
  });

  it('error 事件写进 agentError 且不产生候选', async () => {
    stubFetch(() => agentReply([
      { type: 'error', seq: 1, code: 'generation_failed', message: '模型服务不可用' },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    expect(store.agentError).toBe('模型服务不可用');
    expect(store.candidate).toBeNull();
    expect(store.agentRunning).toBe(false);
  });

  it('空目标不发请求', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('   ', 'create');

    expect(calls).toHaveLength(0);
    // 提示必须指出是哪个框：用户把主题写进「导图标题」时，只说"请输入主题"
    // 会让他以为系统坏了。
    expect(store.status).toContain('输入主题或文本');
  });

  it('没有导图时不允许 edit', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('改一下', 'edit');

    expect(calls).toHaveLength(0);
    expect(store.status).toContain('还没有导图');
  });

  it('流只发 tool_start 就 EOF：报中断，不留「正在与模型协作」', async () => {
    stubFetch(() => agentReply([
      { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 既没有 done 也没有 error，不能当成正常结束
    expect(store.agentError).not.toBe('');
    expect(store.agentError).toContain('中断');
    expect(store.status).toContain('中断');
    expect(store.agentRunning).toBe(false);
    expect(store.busy).toBe(false);
    expect(store.candidate).toBeNull();
  });

  it('状态行显示的是人话，不是工具名', async () => {
    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const store = useMindMapStore();
    const running = store.runGeneration('做一张导图', 'create');

    stream.push({ type: 'tool_start', seq: 1, call_id: 'c1', name: 'rag_query' });
    await vi.waitFor(() => expect(store.stageText).toBe('正在查资料'));

    stream.push({ type: 'tool_end', seq: 2, call_id: 'c1', name: 'rag_query', ok: true, latency_ms: 900 });
    await vi.waitFor(() => expect(store.stageText).toBe('已查好资料'));

    stream.push(doneFrame(content()));
    stream.finish();
    await running;
  });
});

describe('部分完成与诊断', () => {
  it('tool_end 标了 partial 时，状态行说明部分分支未完成', async () => {
    stubFetch(() => agentReply([
      // 后端分片生成的真实形态：ok=true，但分支没展开完
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        latency_ms: 1200,
        result_summary: { planned_branches: 4, expanded_branches: 1, partial: true, diagnostics: [] },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    expect(store.agentWarnings.some(item => item.kind === 'partial')).toBe(true);
    // 只看 ok 的话这里是「候选已就绪」，与全部成功一模一样——正是要修掉的
    expect(store.status).toContain('候选');
    expect(store.status).toContain('部分');
  });

  it('带 diagnostics 时面板列出诊断 message，超过三条折成计数', async () => {
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    const messages = [
      '分支「神经网络」展开失败，已留空',
      '因模型调用额度不足，分支「优化理论」没有展开，已留空',
      '分支「概率图模型」只有 2 个要点，少于要求的 4 个',
      '节点「过拟合」与「泛化误差」重复',
    ];
    stubFetch(() => agentReply([
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        latency_ms: 900,
        result_summary: {
          partial: true,
          diagnostics: messages.map((message, index) => ({
            level: 'warn',
            code: `DIAG_${index}`,
            message,
          })),
        },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();
    // 部分完成标记与诊断正文都要出现在候选区上方
    expect(text).toContain('部分完成');
    expect(text).toContain(messages[0]);
    expect(text).toContain(messages[2]);
    // 前三条之后折成一句计数
    expect(text).not.toContain(messages[3]);
    expect(text).toContain('还有 1 条');
  });

  it('接受候选后告警清空，不再挂在面板上', async () => {
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    stubFetch(() => agentReply([
      {
        type: 'tool_end',
        seq: 1,
        call_id: 'c1',
        name: 'mindmap_generate',
        ok: true,
        result_summary: { partial: true, diagnostics: [] },
      },
      doneFrame(content('候选')),
    ]));
    const store = useMindMapStore();
    await store.runGeneration('做一张导图', 'create');
    expect(store.agentWarnings).toHaveLength(1);

    expect(store.acceptCandidate()).toBe('applied');

    // 候选已经落到画布上，"这份候选缺了什么"再挂着只会让用户以为画布有问题
    expect(store.agentWarnings).toHaveLength(0);
  });
});

describe('候选接受', () => {
  async function prepare() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    await store.runGeneration('做一张图', 'create');
    return store;
  }

  it('接受后落到画布，并只记一条撤销', async () => {
    const store = await prepare();
    const before = store.canUndo;

    expect(store.acceptCandidate()).toBe('applied');

    expect(store.content?.root.label).toBe('候选');
    expect(store.candidate).toBeNull();
    expect(store.dirty).toBe(true);
    expect(before).toBe(false);
    expect(store.canUndo).toBe(true);

    store.undo();
    expect(store.content?.root.label).toBe('原图');
  });

  it('候选生成之后改过树，接受要判过时', async () => {
    const store = await prepare();

    // 模拟用户在等待期间改了节点文字
    store.commitLabel('a', '改过的甲');
    expect(store.clientRevision).toBeGreaterThan(0);

    expect(store.acceptCandidate()).toBe('stale');
    // 画布必须保持用户的修改，不能被候选顶掉
    expect(store.content?.root.children[0].label).toBe('改过的甲');
    expect(store.candidate).not.toBeNull();
  });

  it('有未提交的编辑时接受要判过时', async () => {
    const store = await prepare();
    store.beginEdit('a');

    expect(store.acceptCandidate()).toBe('stale');
    expect(store.candidate).not.toBeNull();
  });

  it('没有候选时接受返回 invalid', () => {
    setActivePinia(createPinia());
    expect(useMindMapStore().acceptCandidate()).toBe('invalid');
  });

  it('候选本身不合法时拒绝应用并清掉', async () => {
    // 候选是模型产出的外部数据：label 长度、引用区间、节点数都可能越界。
    // 不在装上画布之前拦一次，脏数据会先入画布，直到用户点保存才被后端 422
    // 拒绝——那时他已经在这棵树上改了半天。
    const dirty = content('脏候选');
    dirty.root.children[0].label = '超'.repeat(201);
    stubFetch(() => agentReply([doneFrame(dirty)]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');

    await store.runGeneration('做一张图', 'create');
    expect(store.candidate).not.toBeNull();

    expect(store.acceptCandidate()).toBe('invalid');
    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('原图');
    expect(store.status).toContain('候选不合法');
  });

  it('丢弃清空候选但不动画布', async () => {
    const store = await prepare();

    store.discardCandidate();

    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('原图');
  });
});

describe('候选与换图', () => {
  const record = (id: string) => ({ id, version: 1, updated_at: '2024-01-01T00:00:00Z' });

  /** 先在同一张图上产出候选，返回 store，供各用例接上"换图"这一步。 */
  async function prepared() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.loadMap(content('原图'), record('map-a'));
    await store.runGeneration('做一张图', 'create');
    expect(store.candidate?.root.label).toBe('候选');
    return store;
  }

  it('生成候选后 loadMap 打开另一张图，候选被清掉', async () => {
    const store = await prepared();

    store.loadMap(content('另一张'), record('map-b'));

    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('另一张');
    expect(store.mapId).toBe('map-b');
  });

  it('生成候选后切图，接受要判过时', async () => {
    const store = await prepared();

    // 关键：切图不会递增 clientRevision，只有会话号能区分"这是另一张图"
    const revisionBefore = store.clientRevision;
    store.newMap();
    expect(store.clientRevision).toBe(revisionBefore);

    expect(store.acceptCandidate()).toBe('stale');
    // 画布必须保持切图后的样子，候选不能借道 mapId 写进新记录
    expect(store.content).toBeNull();
    expect(store.mapId).toBeNull();
  });

  it('preview 换图同样作废旧候选', async () => {
    const store = await prepared();

    store.preview(content('示例'), 'right');

    expect(store.candidate).toBeNull();
    expect(store.acceptCandidate()).toBe('stale');
  });
});

describe('撤销 / 重做与候选', () => {
  /** 先留一步可撤销的历史，再产出候选：这样撤销与重做都真的动到画布。 */
  async function preparedWithHistory() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    // 改一次节点：撤销栈里才有可撤的那一步
    store.commitLabel('a', '改过的甲');
    await store.runGeneration('做一张图', 'create');
    expect(store.candidate).not.toBeNull();
    return store;
  }

  it('候选就绪后撤销一次，接受要判过时', async () => {
    const store = await preparedWithHistory();

    store.undo();
    // 撤销确实生效：内容回到改之前那棵树
    expect(store.content?.root.children[0].label).toBe('甲');

    // 画布已经退出候选所依据的版本，接受必须被拦下，不能把撤销白做掉
    expect(store.acceptCandidate()).toBe('stale');
    expect(store.content?.root.children[0].label).toBe('甲');
    expect(store.candidate).not.toBeNull();
  });

  it('候选就绪后重做一次，接受同样要判过时', async () => {
    const store = await preparedWithHistory();

    // 先撤销再重做，制造出可重做的状态
    store.undo();
    store.redo();
    expect(store.content?.root.children[0].label).toBe('改过的甲');

    expect(store.acceptCandidate()).toBe('stale');
    expect(store.content?.root.children[0].label).toBe('改过的甲');
  });

  it('空画布接受候选后，撤销与重做都能回到空白', async () => {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    expect(store.content).toBeNull();

    await store.runGeneration('做一张图', 'create');
    expect(store.acceptCandidate()).toBe('applied');
    expect(store.content?.root.label).toBe('候选');

    // 撤销回到接受之前的空画布：pushHistoryOrEmpty 压进去的是字符串 'null'
    store.undo();
    expect(store.content).toBeNull();
    expect(store.selectedId).toBeNull();
    expect(store.editingId).toBeNull();

    // 重做回到候选。'null' 解析出来就是 null，不处理这条路径的话
    // 这一步会把「空画布」从撤销栈里弄丢（缺陷 3）
    store.redo();
    expect(store.content?.root.label).toBe('候选');

    // 于是再撤销仍然回得到空白
    store.undo();
    expect(store.content).toBeNull();
  });
});

describe('候选接受之后的身份', () => {
  it('create 意图接受候选后清空 mapId，撤销只回到接受之前', async () => {
    const store = await openExisting('map-a');
    expect(store.mapId).toBe('map-a');

    stubFetch(() => agentReply([doneFrame(content('新建的图'))]));
    await store.runGeneration('做一张新图', 'create');
    expect(store.acceptCandidate()).toBe('applied');

    // 新作品不能顶着上一张图的身份：否则下一次保存会走 PUT，把 map-a 整条覆盖
    expect(store.mapId).toBeNull();
    expect(store.version).toBe(0);
    expect(store.savedAt).toBeNull();
    expect(store.mode).toBe('model');
    expect(store.dirty).toBe(true);

    // 撤销栈里只有一步：撤销回到接受之前的画布，再撤也没有了
    expect(store.canUndo).toBe(true);
    store.undo();
    expect(store.content?.root.label).toBe('原图');
    expect(store.canUndo).toBe(false);
  });

  it('edit 意图接受候选后沿用原记录的身份', async () => {
    const store = await openExisting('map-a');

    stubFetch(() => agentReply([doneFrame(content('改后'))]));
    await store.runGeneration('把根改名', 'edit');
    expect(store.acceptCandidate()).toBe('applied');

    // 改的就是这张图：保存仍打 map-a 的 PUT，版本还是开图时看到的那个
    expect(store.mapId).toBe('map-a');
    expect(store.version).toBe(3);
    expect(store.savedAt).toBe('2024-01-01T00:00:00Z');
  });
});

describe('候选可见性', () => {
  it('收到 snapshot 帧时候选立刻有值，画布不变', async () => {
    stubFetch(() => agentReply([
      // 后端整树校验通过后先推一帧 snapshot：done 之前候选就该看得见
      { type: 'snapshot', seq: 1, content: content('快照图') },
      // done 故意不带内容：候选只可能来自上面那一帧 snapshot
      { type: 'done', seq: 2 },
    ]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');

    await store.runGeneration('做一张图', 'create');

    expect(store.candidate?.root.label).toBe('快照图');
    // snapshot 只进候选，绝不写画布
    expect(store.content?.root.label).toBe('原图');
    expect(store.agentEvents.map(event => event.type)).toEqual(['snapshot', 'done']);
  });
});

describe('来源进入请求体', () => {
  it('来源随请求原样送出，不塞进 goal', async () => {
    // 远超原来 1000 字的截断线：材料必须整份送到，一个字都不在本地裁
    const material = '材料正文'.repeat(400);
    const source: Source = {
      id: 's1',
      kind: 'text',
      filename: null,
      text: material,
      span: null,
      status: 'parsed',
      error: null,
      parsed_pages: null,
      truncated: false,
      failed_pages: [],
    };
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('请基于附带的 1 份材料生成导图', 'create', [source]);

    const body = calls[0].body as Record<string, unknown>;
    const sent = body.sources as Source[];
    expect(sent).toHaveLength(1);
    expect(Array.from(sent[0].text ?? '')).toHaveLength(Array.from(material).length);
    // goal 是"要做什么"，一个字材料正文都不许混进去
    expect(body.goal).toBe('请基于附带的 1 份材料生成导图');
    expect(String(body.goal)).not.toContain('材料正文');
  });

  it('没有来源时请求体不带 sources', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 空数组也不发：旧后端对多出来的字段是 extra="forbid"
    expect((calls[0].body as Record<string, unknown>).sources).toBeUndefined();
  });
});

describe('思维模型进入请求体', () => {
  it('选中某一套拆解方式时，请求体带 thinking_model', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create', undefined, 'first-principles');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.thinking_model).toBe('first-principles');
  });

  it('没有指定时请求体里是 default，与后端默认值一致', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 两种形态（省略字段 / 显式发 default）选后者并保持一致：排查请求体时
    // 一眼能看出用户选了哪套拆解方式，不用去猜"没这个字段是什么意思"。
    expect((calls[0].body as Record<string, unknown>).thinking_model).toBe('default');
  });

  it('下拉的五个选项与后端标识一致，默认选中通用', async () => {
    // 新行为：下拉只在「会真正生效」时渲染（agent 路径 + 画布为空 / create）。
    // 旧的一次性生成接口与 edit 路径都不消费 thinking_model，所以这里先打开
    // agent 开关，再断言选项；不打开的话下拉根本不出现（见下面两条用例）。
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    const wrapper = mount(SourceInputs, {
      global: { plugins: [createPinia()] },
      props: { confirmDiscard: () => true },
    });

    const select = wrapper.get('#thinking-model');
    const options = select.findAll('option');
    expect(options.map(option => option.attributes('value'))).toEqual([
      'default', 'first-principles', 'feynman', 'systematic', 'critical',
    ]);
    expect(options.map(option => option.text())).toEqual([
      '通用', '第一性原理', '费曼', '5W1H', '批判性',
    ]);
    expect((select.element as HTMLSelectElement).value).toBe('default');
    expect(select.attributes('disabled')).toBeUndefined();
    // 旁边那一行短说明：用户得知道选了它会影响什么
    expect(wrapper.text()).toContain('按这种方式拆解主题');

    // 生成期间与旁边的输入框行为一致：禁用，免得用户改了方式却看不到它生效
    useMindMapStore().busy = true;
    await wrapper.vm.$nextTick();
    expect(select.attributes('disabled')).toBeDefined();
  });

  it('agent 路径关闭时下拉不渲染', () => {
    // 关闭时走旧的一次性生成接口，它根本不接收 thinking_model：
    // 把选不生效的控件摆在这里，用户会以为自己换过拆解方式了。
    const wrapper = mount(SourceInputs, {
      global: { plugins: [createPinia()] },
      props: { confirmDiscard: () => true },
    });

    expect(wrapper.find('#thinking-model').exists()).toBe(false);
  });

  it('已有导图（edit 场景）时下拉不渲染', async () => {
    // 有导图时点击生成走 edit 意图，后端的编辑路径同样不消费 thinking_model。
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    const wrapper = mount(SourceInputs, {
      global: { plugins: [createPinia()] },
      props: { confirmDiscard: () => true },
    });
    expect(wrapper.find('#thinking-model').exists()).toBe(true);

    useMindMapStore().preview(content('原图'), 'right');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('#thinking-model').exists()).toBe(false);
  });
});

describe('面板文案与候选骨架', () => {
  /**
   * 面板只在 agent 路径下可见，所以先打开开关、把状态铺好再挂载，
   * 这样首帧渲染就带着内容，不用等 nextTick 再断言。
   */
  function panel(seed: (store: ReturnType<typeof useMindMapStore>) => void) {
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    const store = useMindMapStore();
    seed(store);
    return mount(AgentPanel);
  }

  it('工具进度显示人话与耗时，不显示工具名', () => {
    const wrapper = panel((store) => {
      store.agentEvents = [
        { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
        { type: 'tool_end', seq: 2, call_id: 'c1', name: 'mindmap_generate', ok: true, latency_ms: 4341 },
        { type: 'tool_start', seq: 3, call_id: 'c2', name: 'node_expand' },
      ];
    });

    const text = wrapper.text();
    expect(text).toContain('已定好框架 · 4.3 秒');
    // 进行中的那一条不带耗时
    expect(text).toContain('正在展开分支');
    // 工程名不该露给用户
    expect(text).not.toContain('mindmap_generate');
    expect(text).not.toContain('node_expand');
  });

  it('候选骨架列出根标题与一级分支，超过 12 条时截断', () => {
    const wrapper = panel((store) => {
      store.candidate = {
        title: '候选',
        root: {
          id: 'root',
          label: '机器学习',
          children: Array.from({ length: 14 }, (_, index) => ({
            id: `n${index}`,
            label: `分支 ${index}`,
            children: [{ id: `n${index}c`, label: '子节点', children: [] }],
          })),
        },
      };
    });

    const text = wrapper.text();
    // 标题要写明"未应用"，否则用户会以为画布已经变了
    expect(text).toContain('候选（未应用）');
    expect(text).toContain('机器学习');
    expect(text).toContain('（1 个子节点）');
    // 只列前 12 条，剩下的折成一句话
    expect(text).toContain('分支 11');
    expect(text).not.toContain('分支 12');
    expect(text).toContain('……还有 2 个分支');
  });
});
