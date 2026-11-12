import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import type { AgentEvent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

function pushEvents(events: AgentEvent[]) {
  const store = useMindMapStore();
  store.agentEvents = events;
  return store;
}

describe('模型原话', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('没产出导图时把模型的原话与思考摆出来', () => {
    pushEvents([
      { type: 'stage', seq: 1, stage: '准备', detail: 'create 意图，1 个工具可用' },
      {
        type: 'done',
        seq: 2,
        content: null,
        mode: 'model',
        text: '导图大概是：机器学习分监督与无监督……',
        reasoning: '看到 mindmap_generate 了，但直接说更简单。',
      },
    ]);

    const wrapper = mount(AgentPanel);
    const panel = wrapper.text();

    expect(panel).toContain('模型这次只回了文字，没有调用工具');
    expect(panel).toContain('机器学习分监督与无监督');
    expect(panel).toContain('直接说更简单');
  });

  it('有产出时不出现这一段', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '', reasoning: '' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).not.toContain('模型这次只回了文字');
  });

  it('只有正文没有思考时，思考那块不渲染', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '只说了一句话', reasoning: '' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).toContain('只说了一句话');
    expect(wrapper.text()).not.toContain('模型当时在想什么');
  });

  it('正文为空但思考有内容时，也能看出它在想什么', () => {
    pushEvents([
      { type: 'done', seq: 1, content: null, mode: 'model', text: '', reasoning: '还是直接回答吧' },
    ]);

    const wrapper = mount(AgentPanel);
    expect(wrapper.text()).toContain('（回复里没有正文）');
    expect(wrapper.text()).toContain('还是直接回答吧');
  });
});
