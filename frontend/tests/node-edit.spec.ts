import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import MindNode from '../src/canvas/MindNode.vue';
import type { MindNodeData } from '../src/stores/mindmap';

const data: MindNodeData = {
  label: '截断后的短标题…',
  body: '完整标题全文',
  fullLabel: '完整标题全文',
  depth: 1,
  out: ['right'],
  in: ['left'],
};

function render(props: Record<string, unknown> = {}) {
  return mount(MindNode, {
    props: { id: 'n1', data, ...props },
    // 句柄组件依赖 Vue Flow 的画布上下文，这里只关心节点自身的编辑行为，替换成空壳。
    global: { stubs: { Handle: true } },
    attachTo: document.body,
  });
}

describe('节点内联编辑', () => {
  it('平时只显示截断后的短标题', () => {
    const wrapper = render();

    expect(wrapper.find('input').exists()).toBe(false);
    expect(wrapper.text()).toContain('截断后的短标题…');
  });

  it('进入编辑时输入框里是完整标题', () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    expect((input.element as HTMLInputElement).value).toBe('完整标题全文');
    wrapper.unmount();
  });

  it('回车提交新文字', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('改过的标题');
    await input.trigger('keydown', { key: 'Enter' });

    expect(wrapper.emitted('commit')).toEqual([['改过的标题']]);
    expect(wrapper.emitted('cancel')).toBeUndefined();
    wrapper.unmount();
  });

  it('Esc 取消，不提交', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('不要这个');
    await input.trigger('keydown', { key: 'Escape' });

    expect(wrapper.emitted('cancel')).toHaveLength(1);
    expect(wrapper.emitted('commit')).toBeUndefined();
    wrapper.unmount();
  });

  it('失焦也算提交，且不会因为回车后再失焦重复提交一次', async () => {
    const wrapper = render({ editing: true });

    const input = wrapper.get('input');
    await input.setValue('失焦提交');
    await input.trigger('blur');

    expect(wrapper.emitted('commit')).toEqual([['失焦提交']]);
    wrapper.unmount();
  });
});
