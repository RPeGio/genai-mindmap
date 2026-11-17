/**
 * 外部来源的引用要能点回原文（`Source.metadata.url` / `SourceUsage.url`）。
 *
 * 这一层的价值不在"渲染了一个 `<a>`"，而在**两种来源都要能点**：
 *
 *  - 本次会话里解析过的来源：`Source.metadata.url`（最完整）；
 *  - 从已保存的导图打开：手里没有 `Source`，只有 `source_usage`——把 url 只留在
 *    `metadata` 上，等于"这一轮能点、下次打开就没了"。
 *
 * 后者正是这条链最容易漏的地方（与 `note` / `source_refs` 被整批丢掉是同一类
 * 教训），所以它单独一条用例。
 */
import { mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, Source, SourceRef, SourceUsage } from '../src/api/types';
import { refUrl } from '../src/mindmap/refs';
import { SOURCE_LABELS } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；
// 界面测试只关心详情面板，画布替换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

function mountApp() {
  return mount(App, { global: { plugins: [createPinia()] } });
}

const REF: SourceRef = { source_id: 's1', start: 0, end: 3, quote: '碳中和' };

function contentWith(refs: SourceRef[], usage: SourceUsage[] = []): MindMapContent {
  return {
    title: '碳中和',
    source_usage: usage,
    root: {
      id: 'r',
      label: '碳中和',
      children: [{ id: 'a', label: '政策', children: [], source_refs: refs }],
    },
  };
}

/** 一次会话内的外部来源（`kind` 是 web/paper，`metadata.url` 是原文地址）。 */
function externalSource(url: string | null): Source {
  return {
    id: 's1',
    kind: 'web',
    filename: 'wikipedia: 碳中和',
    text: '碳中和是指在一定时间内抵消自身产生的二氧化碳排放量。',
    span: null,
    status: 'parsed',
    error: null,
    parsed_pages: null,
    truncated: false,
    failed_pages: [],
    metadata: { url, tags: ['wikipedia'] },
  };
}

/** 把一份外部来源塞进本次会话（`readySources` 由 `items` 派生）。 */
function mountSource(source: Source): void {
  useSourcesStore().items.push({
    key: source.id,
    id: source.id, label: source.filename ?? '外部来源', kind: source.kind,
    state: 'ready', source, error: '', seconds: 0,
  });
}

describe('外部来源的引用链接', () => {
  it('metadata.url 非空时渲染成新窗口打开的链接', async () => {
    const wrapper = mountApp();
    mountSource(externalSource('https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C'));

    const store = useMindMapStore();
    store.preview(contentWith([REF]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const link = wrapper.get('a[href="https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C"]');
    expect(link.attributes('target')).toBe('_blank');
    // `noopener noreferrer` 不是可选项：外部页面能通过 `window.opener` 反向操作本页。
    expect(link.attributes('rel')).toBe('noopener noreferrer');
    // 显示 `source_name: title`，用户一眼能看出这是哪个源的哪一条
    expect(link.text()).toContain('wikipedia: 碳中和');
    // 来源类型的中文标签也要在（kind=web → 网页）
    expect(link.text()).toContain(SOURCE_LABELS.web);
  });

  it('url 为空时不渲染链接，引用仍然能点开抽屉看摘录', async () => {
    const wrapper = mountApp();
    mountSource(externalSource(null));

    const store = useMindMapStore();
    store.preview(contentWith([REF]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('a[target="_blank"]').exists()).toBe(false);
    const button = wrapper.findAll('button').find(item => item.text().includes('碳中和'));
    expect(button).toBeTruthy();
  });

  it('从已保存的导图打开时用 source_usage 里的 url', async () => {
    // 这条钉的是"保存后重开还能点"：此时 `useSourcesStore` 是空的，唯一的地址
    // 来源就是导图自己的 `source_usage`。
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(contentWith([REF], [{
      source_id: 's1', kind: 'paper', filename: 'arxiv: Carbon Neutrality',
      char_count: 120, url: 'https://arxiv.org/abs/2310.01234v1',
    }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    const link = wrapper.get('a[href="https://arxiv.org/abs/2310.01234v1"]');
    expect(link.text()).toContain('arxiv: Carbon Neutrality');
    expect(link.text()).toContain(SOURCE_LABELS.paper);
  });

  it('refUrl 只放行 http/https', () => {
    const usage = (url: string | null): SourceUsage[] => [
      { source_id: 's1', kind: 'web', filename: 'x', char_count: 1, url },
    ];
    expect(refUrl(REF, null, usage('https://example.com/a'))).toBe('https://example.com/a');
    expect(refUrl(REF, null, usage('http://example.com/a'))).toBe('http://example.com/a');
    expect(refUrl(REF, null, usage('javascript:alert(1)'))).toBeNull();
    expect(refUrl(REF, null, usage(null))).toBeNull();
    expect(refUrl(REF, null, [])).toBeNull();
  });
});
