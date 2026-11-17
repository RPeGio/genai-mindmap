import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, Source } from '../src/api/types';
import { textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

/**
 * 画布右上角那个框（合并后的 #2）：**唯一**说一句话的地方。
 *
 * 合并之前有两个文字入口：左侧「输入主题或文本」+「合并生成导图」（有图时发
 * `edit` 并**清空**选区），画布上方「改这张图」（发 `edit` 并**带上**选区）。
 * 同一个意图、两处入口、选区语义相反，而文案上看不出区别。这一组用例把合并后的
 * 契约钉死：三态文案、两个意图的请求体、只给材料时的兜底、键盘。
 */

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const INTENT = 'textarea#intent';
const BOX = '[data-test="intent-box"]';

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

function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  }));
  return calls;
}

const BASE: MindMapContent = {
  title: '示例',
  root: {
    id: 'root', label: '根', children: [
      { id: 'a', label: '甲', children: [] },
      { id: 'b', label: '乙', children: [] },
      { id: 'c', label: '丙', children: [] },
    ],
  },
};

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(App, { global: { plugins: [pinia] } });
}

/** 输入框：拿到原生元素断言 placeholder / value。 */
function intentField(wrapper: VueWrapper) {
  return wrapper.get(INTENT).element as HTMLTextAreaElement;
}

function submitButton(wrapper: VueWrapper) {
  return wrapper.get(`${BOX} button`);
}

function boxText(wrapper: VueWrapper): string {
  return wrapper.get(BOX).text();
}

/** 一份解析好的文档来源。`filename` 决定它能不能当主题。 */
function material(filename: string, text = '材料正文'): Source {
  return {
    ...textSource(text),
    id: `doc-${filename}`,
    kind: 'document',
    filename,
    status: 'parsed',
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('三态文案与按钮', () => {
  it('没图：输入主题 + 「生成导图」，不显示选区徽标', () => {
    const wrapper = mountApp();

    expect(intentField(wrapper).placeholder).toBe('输入主题，或直接点生成用已上传的材料');
    expect(submitButton(wrapper).text()).toBe('生成导图');
    expect(wrapper.find('[data-test="edit-selection"]').exists()).toBe(false);
    // 这个框在这一态的名字是「主题或要求」：合并之后它是唯一的主题入口
    expect(wrapper.get(`${BOX} label`).text()).toBe('主题或要求');
    // 只影响新建的两个选项跟着出现
    expect(wrapper.find('#thinking-model').exists()).toBe(true);
    expect(wrapper.find('#diagram-type').exists()).toBe(true);
    // 同一个框里不重复两遍"写一个主题"：初始状态行让位给提示
    const shown = boxText(wrapper).split('写一个主题').length - 1;
    expect(shown).toBe(1);
  });

  it('有图 · 没选节点：文案变成「说说你想怎么改」，按钮变成「改这张图」', async () => {
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    expect(intentField(wrapper).placeholder).toBe('说说你想怎么改');
    expect(submitButton(wrapper).text()).toBe('改这张图');
    expect(wrapper.find('[data-test="edit-selection"]').exists()).toBe(false);
    expect(wrapper.get(`${BOX} label`).text()).toBe('怎么改');
    // 编辑路径不生成骨架：这两个选项在这一态不生效，就不该摆出来
    expect(wrapper.find('#thinking-model').exists()).toBe(false);
    expect(wrapper.find('#diagram-type').exists()).toBe(false);
    expect(boxText(wrapper)).toContain('先在画布上选中节点，或直接描述要改哪里');
  });

  it('有图 · 选了 3 个节点：徽标、placeholder、按钮都跟着数走', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    store.toggleSelect('b');
    store.toggleSelect('c');
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="edit-selection"]').text()).toBe('已选 3 个节点');
    expect(intentField(wrapper).placeholder).toBe('改选中的这 3 个节点…');
    expect(submitButton(wrapper).text()).toBe('改这 3 个节点');
    // 提示语要说明"选区是提示不是限制"，否则用户会以为自己把模型锁住了
    expect(boxText(wrapper)).toContain('优先改这 3 个节点及其子分支');
  });

  it('只选一个节点时用单数文案', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="edit-selection"]').text()).toBe('已选 1 个节点');
    expect(intentField(wrapper).placeholder).toBe('改选中的这个节点…');
    expect(submitButton(wrapper).text()).toBe('改这个节点');
  });
});

describe('点按钮发出什么', () => {
  it('没图：发 create，不带快照，也不带选区', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('机器学习的主要方向');
    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('create');
    expect(body.goal).toBe('机器学习的主要方向');
    expect(body.snapshot).toBeNull();
    expect(body.selected_ids).toEqual([]);
    // 发完就清空：这个框不是"材料"，留着上一句话只会让人以为它还会再生效
    expect(intentField(wrapper).value).toBe('');
  });

  it('有图没选节点：发 edit，带整棵树，选区为空', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    await wrapper.get(INTENT).setValue('整张图都加上时间维度');
    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect(body.goal).toBe('整张图都加上时间维度');
    expect((body.snapshot as MindMapContent).root.id).toBe('root');
    expect(body.selected_ids).toEqual([]);
  });

  it('有图选了 3 个：请求体里的 selected_ids 正好是这三条，顺序也一样', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    store.toggleSelect('b');
    store.toggleSelect('c');
    await wrapper.vm.$nextTick();

    await wrapper.get(INTENT).setValue('这三个合并成一个');
    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    // 第一个是主选区（详情面板对着的那个），后两个是追加的
    expect(body.selected_ids).toEqual(['a', 'b', 'c']);
  });

  it('没写主题时按钮点不动，也不发请求', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(0);
  });

  it('有图但没写要改什么：按钮同样点不动（改图必须说一句话）', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(0);
  });
});

/**
 * 只给材料、不给主题（合并后最常见的路径）。
 *
 * 选了 **B**：允许没有主题，用第一份材料的标题当主题。判据在 `materialTitle`
 * 里（长度、通用词、文件编号），因为拿「未命名」当中心主题只会生成一张垃圾图 ——
 * 那种时候宁可退回"请写一个主题"。
 */
describe('只给材料的场景', () => {
  it('材料标题能用时：不写主题也能生成，标题写进 goal', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      id: 's1', label: '机器学习导论.pdf', kind: 'document', state: 'ready',
      source: material('机器学习导论.pdf'), error: '', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    // 先告诉用户"你还没写主题，我会用哪个"——比点完再解释好
    expect(boxText(wrapper)).toContain('没写主题：用材料标题「机器学习导论」当中心主题');
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined();

    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('create');
    // 模型看不见文件名：标题必须写进 goal 才算"用了它当主题"
    expect(body.goal).toContain('机器学习导论');
    expect(body.sources).toHaveLength(1);
  });

  it('标题是通用词（未命名.txt）：不兜底，退回"写一个主题"', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      id: 's1', label: '未命名.txt', kind: 'document', state: 'ready',
      source: material('未命名.txt'), error: '', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    expect(boxText(wrapper)).toContain('材料标题「未命名」是通用词');
    expect(boxText(wrapper)).toContain('写一个主题');
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();

    await submitButton(wrapper).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);

    // 补一句主题就能生成：材料还在，没被丢掉
    await wrapper.get(INTENT).setValue('整理一下这份材料');
    expect(submitButton(wrapper).attributes('disabled')).toBeUndefined();
  });

  it('粘贴的文本没有标题：同样要求写主题', async () => {
    const wrapper = mountApp();
    useSourcesStore().addText('一段很长的材料正文');
    await wrapper.vm.$nextTick();

    expect(boxText(wrapper)).toContain('粘贴的材料没有标题');
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
  });

  it('第一份标题不可用时往后找：用第二份的标题', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [
      {
        id: 's1', label: 'IMG_2041.png', kind: 'image', state: 'ready',
        source: material('IMG_2041.png'), error: '', seconds: 0,
      },
      {
        id: 's2', label: '碳中和路线图.pdf', kind: 'document', state: 'ready',
        source: material('碳中和路线图.pdf'), error: '', seconds: 0,
      },
    ];
    await wrapper.vm.$nextTick();

    expect(boxText(wrapper)).toContain('材料标题「碳中和路线图」');
  });

  it('材料还在解析、或有失败项时按钮禁用，并说明原因', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      id: 's1', label: '算法导论.pdf', kind: 'document', state: 'failed',
      source: null, error: '文档解析失败', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
    expect(boxText(wrapper)).toContain('有来源解析失败');
  });

  it('录音期间不能生成：那一段还没变成材料', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      id: 's1', label: '算法导论.pdf', kind: 'document', state: 'ready',
      source: material('算法导论.pdf'), error: '', seconds: 0,
    }];
    sources.recording = true;
    await wrapper.vm.$nextTick();

    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
    expect(boxText(wrapper)).toContain('正在录音');
  });
});

describe('键盘', () => {
  it('Enter 提交', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('机器学习');
    await wrapper.get(INTENT).trigger('keydown', { key: 'Enter' });
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(1);
    expect((calls[0].body as Record<string, unknown>).intent).toBe('create');
  });

  it('Shift+Enter 换行，不提交', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('第一行');
    await wrapper.get(INTENT).trigger('keydown', { key: 'Enter', shiftKey: true });
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(0);
    // 内容还在框里：换行不该把半句话发出去
    expect(intentField(wrapper).value).toBe('第一行');
  });

  it('输入法组词时的回车不算提交（中文主题必经一步）', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();

    await wrapper.get(INTENT).setValue('机器');
    await wrapper.get(INTENT).trigger('keydown', { key: 'Enter', isComposing: true });
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(0);
  });
});

describe('材料区只剩材料', () => {
  it('没有输入主题的框，也没有「合并生成导图」按钮', () => {
    const wrapper = mountApp();
    // 合并前这两个都在左侧栏里；现在同一个意图只在一个地方
    expect(wrapper.find('textarea#prompt').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('合并生成导图');
    expect(wrapper.text()).not.toContain('输入主题或文本');
    // 材料区该有的东西一件不少
    expect(wrapper.find('#material-text').exists()).toBe(true);
    expect(wrapper.text()).toContain('添加文本到来源');
    expect(wrapper.text()).toContain('添加文件（可多选）');
    expect(wrapper.text()).toContain('录音添加来源');
    expect(wrapper.text()).toContain('来源列表（0/50）');
  });

  it('材料区的说明写清它是干什么的，并交代"不传材料会去外部搜资料"', () => {
    const wrapper = mountApp();
    const panel = wrapper.get('main section').text();
    expect(panel).toContain('材料（可选）');
    expect(panel).toContain('上传文档、粘贴文字或录音');
    expect(panel).toContain('不传材料就按主题生成（会去外部搜资料）');
    // 老用户习惯"在左侧框里打字然后点生成"：这一句是防误用的关键
    expect(panel).toContain('写主题、说「怎么改」在画布右上角那个框');
  });

  it('粘贴的材料只有在点「添加文本到来源」之后才算来源', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();

    await wrapper.get('#material-text').setValue('一段材料正文');
    expect(sources.items).toHaveLength(0);

    await wrapper.findAll('button').find(item => item.text() === '添加文本到来源')!.trigger('click');
    expect(sources.items).toHaveLength(1);
    expect((wrapper.get('#material-text').element as HTMLTextAreaElement).value).toBe('');
    expect(wrapper.text()).toContain('来源列表（1/50）');
  });

  it('清空来源把材料清空，计数归零', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('一');
    sources.addText('二');
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain('来源列表（2/50）');

    await wrapper.findAll('button').find(item => item.text() === '清空来源')!.trigger('click');
    expect(sources.items).toHaveLength(0);
    expect(wrapper.text()).toContain('来源列表（0/50）');
  });
});
