import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, Source } from '../src/api/types';
import SourceInputs from '../src/components/SourceInputs.vue';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

function source(id: string, filename = '线代第一章.pdf'): Source {
  return {
    id,
    kind: 'document',
    filename,
    text: '矩阵的秩等于行向量组的极大线性无关组的个数。初等变换不改变矩阵的秩。',
    span: null,
    status: 'parsed',
    error: null,
    parsed_pages: 22,
    truncated: false,
    failed_pages: [],
  };
}

/** 两个来源：s1 被两个节点引用，s2 一次都没被引用。 */
function content(): MindMapContent {
  return {
    title: '线代',
    root: {
      id: 'root',
      label: '线代复习',
      children: [
        {
          id: 'a',
          label: '矩阵的秩',
          source_refs: [{ source_id: 's1', start: 0, end: 6, quote: '矩阵的秩' }],
          children: [
            {
              id: 'a1',
              label: '极大无关组',
              source_refs: [{ source_id: 's1', start: 6, end: 20 }],
              children: [],
            },
          ],
        },
        { id: 'b', label: '初等变换', children: [] },
      ],
    },
    source_usage: [
      { source_id: 's1', kind: 'document', filename: '线代第一章.pdf', char_count: 34 },
      { source_id: 's2', kind: 'document', filename: '线代第二章.pdf', char_count: 34 },
    ],
  };
}

function ready(id: string, label: string) {
  const sources = useSourcesStore();
  sources.items.push({ id, label, kind: 'document', state: 'ready', source: source(id), error: '', seconds: 0 });
}

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(SourceInputs, {
    global: { plugins: [pinia] },
    props: { confirmDiscard: () => true },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('来源行上的反向引用', () => {
  it('没有导图时不提示"没被引用"', () => {
    const wrapper = mountPanel();
    ready('s1', 'a.pdf');
    ready('s2', 'b.pdf');

    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.text()).not.toContain('没有被任何节点引用');
      expect(wrapper.text()).not.toContain('个节点引用');
    });
  });

  it('有导图后显示被几个节点引用，点开能看节点列表', async () => {
    const wrapper = mountPanel();
    ready('s1', 'a.pdf');
    ready('s2', 'b.pdf');
    useMindMapStore().preview(content(), 'right');
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('被 2 个节点引用');
    // 未被引用的那份单独标出来
    expect(wrapper.text()).toContain('没有被任何节点引用');
    expect(wrapper.text()).toContain('矩阵的秩');
    expect(wrapper.text()).toContain('极大无关组');
  });

  it('点节点列表里的标题会选中该节点并请求居中', async () => {
    const wrapper = mountPanel();
    ready('s1', 'a.pdf');
    const store = useMindMapStore();
    store.preview(content(), 'right');
    await wrapper.vm.$nextTick();

    const before = store.focusTick;
    const target = wrapper.findAll('button').find(button => button.text() === '极大无关组');
    expect(target).toBeTruthy();
    await target!.trigger('click');

    expect(store.selectedId).toBe('a1');
    expect(store.focusTick).toBe(before + 1);
  });
});

describe('store 里的反向索引与跳转', () => {
  it('只统计 content 里的节点，预览树不算', async () => {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    expect(store.refIndex.counts.s1).toBe(2);
    expect(store.refIndex.counts.s2).toBeUndefined();
  });

  it('跳转到不存在的节点不生效，也不请求居中', () => {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    const before = store.focusTick;

    store.locate('preview-only');

    expect(store.selectedId).toBeNull();
    expect(store.focusTick).toBe(before);
  });

  it('同一条引用连点两次，居中请求每次都会触发', () => {
    const store = useMindMapStore();
    store.preview(content(), 'right');
    const before = store.focusTick;

    store.locate('a');
    store.locate('a');

    expect(store.focusTick).toBe(before + 2);
  });
});
