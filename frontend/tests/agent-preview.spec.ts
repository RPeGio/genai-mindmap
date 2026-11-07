import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 手动推进的 SSE 响应。
 *
 * 预览只在生成过程中存在：一次性返回的流一结束就收尾清空了，断言不到「长到一半
 * 时界面上是什么」。
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

/** 一帧 node_grow。seq 只是信封要求，前端不靠它排序。 */
function grow(path: number[] | null, label: string, expandable: boolean | null = null) {
  return { type: 'node_grow', seq: 1, path, label, expandable };
}

function content(label: string): MindMapContent {
  return {
    title: '示例',
    root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
  };
}

/** 开一条可控的 agent 流：返回推帧、收尾两个手柄和运行中的 promise。 */
function startStream(goal = '做一张导图') {
  const stream = controllableReply();
  vi.stubGlobal('fetch', vi.fn(async () => stream.response));
  const store = useMindMapStore();
  const running = store.runGeneration(goal, 'create');
  return { stream, store, running };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('预览树按 path 生长', () => {
  it('根帧建立这棵树，其余节点挂到 path 指向的父节点下', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '机器学习'));
    // path 指向父节点：[] 表示挂在根上，[0] 表示根的第 0 个孩子
    stream.push(grow([], '监督学习'));
    stream.push(grow([], '无监督学习'));
    stream.push(grow([0], '线性回归'));
    stream.push(grow([0, 0], '最小二乘'));

    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(2));

    expect(store.agentPreview?.label).toBe('机器学习');
    expect(store.agentPreview?.children[1].label).toBe('无监督学习');
    expect(store.agentPreview?.children[0].children[0].label).toBe('线性回归');
    expect(store.agentPreview?.children[0].children[0].children[0].label).toBe('最小二乘');
    // 无监督学习这一支还没长东西，不能凭空多出子节点
    expect(store.agentPreview?.children[1].children).toHaveLength(0);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('并发的几路交错到达时，各自归到自己的父节点下', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], 'A'));
    stream.push(grow([], 'B'));
    // 两条子帧交错：B 的孩子先到，A 的孩子后到
    stream.push(grow([1], 'B 的孩子'));
    stream.push(grow([0], 'A 的孩子'));

    await vi.waitFor(() => expect(store.agentPreview?.children[1].children).toHaveLength(1));

    expect(store.agentPreview?.children[0].children[0].label).toBe('A 的孩子');
    expect(store.agentPreview?.children[1].children[0].label).toBe('B 的孩子');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('同一 (path, label) 重复到达只挂一个节点', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    // 重连重放、后端补发都会让同一帧再来一次
    stream.push(grow([], '分支'));
    stream.push(grow([], '分支'));
    // 前后空白只是传输噪音，不该因此多出一个同名兄弟
    stream.push(grow([], ' 分支 '));

    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));
    expect(store.agentPreview?.children[0].label).toBe('分支');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });
});

describe('乱序与丢帧', () => {
  it('父节点还没到时先攒着，父节点一到就挂上去', async () => {
    const { stream, store, running } = startStream();
    // 父节点是根的第 0 个孩子的第 1 个孩子：此刻树还没建起来
    stream.push(grow([0, 1], '迟到的孙子'));
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支一'));
    stream.push(grow([0], '甲'));
    expect(store.agentPreviewDropped).toBe(0);

    stream.push(grow([0], '乙'));

    await vi.waitFor(() =>
      expect(store.agentPreview?.children[0].children[1].children[0].label).toBe('迟到的孙子'));

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('path 索引越界时不崩、不塞占位节点，父节点补上后仍然能挂', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    // 根现在一个孩子都没有，索引 2 越界：不能抛异常，更不能用空占位把位置撑出来
    stream.push(grow([2], '第六个分支'));

    await vi.waitFor(() => expect(store.agentPreview?.label).toBe('根'));
    expect(store.agentPreview?.children).toHaveLength(0);

    for (const label of ['一', '二', '三', '四', '五', '六']) stream.push(grow([], label));

    // 索引 2 对应的父节点（第三个孩子）到位后，之前越界的那帧才挂上去
    await vi.waitFor(() =>
      expect(store.agentPreview?.children[2].children[0].label).toBe('第六个分支'));
    expect(store.agentPreview?.children).toHaveLength(6);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('不可能合法的 path 直接丢弃，并留下条数可查', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([-1], '负索引'));
    // 比导图允许的层数还深，永远不可能挂上，攒着只是白等
    stream.push(grow([0, 0, 0, 0, 0, 0, 0, 0], '太深了'));

    await vi.waitFor(() => expect(store.agentPreviewDropped).toBe(2));
    expect(store.agentPreview?.children).toHaveLength(0);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('label 为空或超长的帧丢弃，不建出没有标题的节点', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '   '));
    stream.push(grow([], '超'.repeat(201)));

    await vi.waitFor(() => expect(store.agentPreviewDropped).toBe(2));
    expect(store.agentPreview?.children).toHaveLength(0);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('expandable 原样保留在预览节点上，null 不等于 false', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '还能拆', true));
    stream.push(grow([], '拆到底了', false));
    stream.push(grow([], '模型还没写', null));

    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(3));
    expect(store.agentPreview?.children[0].expandable).toBe(true);
    expect(store.agentPreview?.children[1].expandable).toBe(false);
    expect(store.agentPreview?.children[2].expandable).toBeNull();

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('根帧丢了时，挂在根上的分支不会被当成中心主题', async () => {
    const { stream, store, running } = startStream();
    // 这两种帧的 path 在旧协议里都是 []，只能靠「根必须最先到」来区分。那个依赖
    // 是隐式的：顺序一变，第一个分支就会顶替中心主题，而且不会报任何错。
    stream.push(grow([], '假根'));
    stream.push(grow([], '也是假的'));
    // 拿一个必然被丢弃的帧当同步点：它处理完，说明上面两帧也已经走过了
    stream.push(grow([], '   '));
    await vi.waitFor(() => expect(store.agentPreviewDropped).toBe(1));

    // 根没来，它们就只能等着——宁可整棵树不长，也不能长出一棵根错了的树
    expect(store.agentPreview).toBeNull();

    stream.push(grow(null, '真根'));
    await vi.waitFor(() => expect(store.agentPreview?.label).toBe('真根'));
    expect(store.agentPreview?.children.map(child => child.label))
      .toEqual(['假根', '也是假的']);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });
});

describe('预览与候选的隔离', () => {
  it('预览不进候选，只有预览时也没有可采纳的内容', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));

    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));
    expect(store.candidate).toBeNull();
    // 预览是没校验过的半成品，接受动作只能认 snapshot / done 给的候选
    expect(store.acceptCandidate()).toBe('invalid');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('snapshot 到达时用完整候选替换预览，缓存一并清空', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));
    // 这条帧的父节点一直没来：它躺在缓存里，snapshot 之后也不能再冒出来
    stream.push(grow([0, 3], '永远等不到父的节点'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });

    await vi.waitFor(() => expect(store.candidate?.root.label).toBe('快照根'));
    expect(store.agentPreview).toBeNull();

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;

    // 缓存清空了：再补一帧也不会把上一轮的孤儿挂到已经作废的树上
    expect(store.agentPreview).toBeNull();
  });

  it('error 到达时预览作废', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    stream.push({ type: 'error', seq: 8, code: 'generation_failed', message: '模型服务不可用' });

    await vi.waitFor(() => expect(store.agentPreview).toBeNull());
    expect(store.agentError).toBe('模型服务不可用');

    stream.finish();
    await running;
  });

  it('用户取消后预览作废，面板不再挂着半截树', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    store.cancel();

    expect(store.agentPreview).toBeNull();
    expect(store.agentRunning).toBe(false);

    stream.finish();
    await running;
    expect(store.agentPreview).toBeNull();
  });

  it('连接断掉后残留的预览，下一轮开始时清掉', async () => {
    const { stream, store, running } = startStream('第一轮');
    stream.push(grow(null, '上一轮的根'));
    await vi.waitFor(() => expect(store.agentPreview?.label).toBe('上一轮的根'));

    // 没有 done / error 就 EOF：连接断了，预览没有终态帧来作废它
    stream.finish();
    await running;
    expect(store.agentError).toContain('中断');
    expect(store.agentPreview?.label).toBe('上一轮的根');

    const second = startStream('第二轮');
    // 新一轮从清空开始：上一轮的节点混进来会让人以为这次已经长出了这些分支
    expect(second.store.agentPreview).toBeNull();
    expect(second.store.agentPreviewDropped).toBe(0);

    second.stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    second.stream.finish();
    await second.running;
  });
});

describe('面板上的预览', () => {
  /** 面板只在 agent 路径下渲染，所以先把开关打开再取 store。 */
  function openPanel() {
    vi.stubEnv('VITE_MINDMAP_AGENT', '1');
    return startStream();
  }

  /** 按标题找到预览里那一行。 */
  function row(wrapper: ReturnType<typeof mount>, label: string) {
    const hit = wrapper.findAll('li').find(item => item.text().includes(label));
    if (!hit) throw new Error(`面板上没有这一行：${label}`);
    return hit;
  }

  it('生成中列出预览树，明确不能拆的节点才有标记', async () => {
    const { stream, store, running } = openPanel();
    stream.push(grow(null, '机器学习'));
    stream.push(grow([], '监督学习', false));
    stream.push(grow([0], '线性回归', true));
    stream.push(grow([], '还没定', null));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(2));

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();
    expect(text).toContain('生成中（预览）');
    expect(text).toContain('机器学习');
    expect(text).toContain('线性回归');
    // 预览是半成品，必须写明以候选为准，否则用户会当作可应用的结果
    expect(text).toContain('以最终候选为准');

    // expandable 为 false 才带小圆点；true 与 null 都不加标记
    expect(row(wrapper, '监督学习').find('span[aria-hidden="true"]').exists()).toBe(true);
    expect(row(wrapper, '线性回归').find('span[aria-hidden="true"]').exists()).toBe(false);
    expect(row(wrapper, '还没定').find('span[aria-hidden="true"]').exists()).toBe(false);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('挂载之后到达的帧会让面板长出新的行', async () => {
    const { stream, store, running } = openPanel();
    stream.push(grow(null, '根'));
    await vi.waitFor(() => expect(store.agentPreview?.label).toBe('根'));

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).not.toContain('后来的一支');

    stream.push(grow([], '后来的一支'));

    // 预览树是就地增长的：面板得跟着重新渲染，否则用户盯着看只能看到根
    await vi.waitFor(() => expect(wrapper.text()).toContain('后来的一支'));

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('候选到达后切到完整候选，预览不再出现', async () => {
    const { stream, store, running } = openPanel();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '只在预览里的分支'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.candidate?.root.label).toBe('快照根'));

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();
    expect(text).toContain('候选（未应用）');
    expect(text).toContain('快照根');
    expect(text).not.toContain('生成中（预览）');
    // 预览独有的节点不能残留：它不在最终候选里，出现就是两棵树拼在了一起
    expect(text).not.toContain('只在预览里的分支');

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('预览很长时只列前 24 行，剩下的折成计数', async () => {
    const { stream, store, running } = openPanel();
    stream.push(grow(null, '根'));
    for (let index = 0; index < 30; index += 1) stream.push(grow([], `分支 ${index}`));

    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(30));

    const wrapper = mount(AgentPanel);
    const text = wrapper.text();
    expect(text).toContain('分支 22');
    expect(text).not.toContain('分支 23');
    expect(text).toContain('……还有 7 个节点');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });
});
