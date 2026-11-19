import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { DiagramType, MindMapContent, MindNode } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 「附加关系」区块的可见性由**产物自己的图型**决定。
 *
 * 这个区块此前一直摆着：它只对流程图有意义（思维导图与鱼骨图的边为空是正常的，
 * 不是"这个节点没有关系"），但代码里没有任何判断依据，只能一直显示。加在
 * `MindMapContent.diagram_type` 上之后，界面第一次能自己判断。
 *
 * 三条边界一起钉：
 *   1. `flowchart` → 显示（模型会给边，用户要能补、能删）；
 *   2. `tree` / `fishbone` / **旧图（没有这个字段）** → 不显示；
 *   3. 手里**已经有边**时一律显示——只按图型判会让旧图里那些边再也删不掉
 *      （列表与删除按钮都在这个区块里）。
 */

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；与 `app.spec.ts`
// 同一个做法：画布换成占位元素，只看侧栏。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const SECTION = 'section[aria-label="附加关系"]';

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id, label, children,
});

function content(diagram_type?: DiagramType): MindMapContent {
  return {
    title: '图',
    root: node('root', '主题', [node('a', '甲'), node('b', '乙')]),
    // 不传 `diagram_type` 就是**旧图**：字段缺失等价于 `tree`，而这条等价关系
    // 正是后端那个字段默认值的全部含义。
    ...(diagram_type ? { diagram_type } : {}),
  };
}

/** 挂一张图、选中一个节点（详情面板只在有选中时渲染），返回整个 App。 */
function mountWith(data: MindMapContent) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useMindMapStore();
  store.preview(data, 'right');
  store.select('a');
  return mount(App, { global: { plugins: [pinia] } });
}

describe('附加关系区块按图型显示', () => {
  it('流程图显示：模型会给边，而补错的、删画歪的都要有入口', () => {
    const wrapper = mountWith(content('flowchart'));
    expect(wrapper.find(SECTION).exists()).toBe(true);
    expect(wrapper.find(SECTION).text()).toContain('连一条关系');
  });

  it('思维导图不显示：那上面"没有附加关系"是定义，不是缺陷', () => {
    expect(mountWith(content('tree')).find(SECTION).exists()).toBe(false);
  });

  it('鱼骨图不显示：它的六条一级分支是固定的，没有跨支跳转可画', () => {
    expect(mountWith(content('fishbone')).find(SECTION).exists()).toBe(false);
  });

  it('旧图（没有 diagram_type 字段）按 tree 处理，不显示', () => {
    expect(mountWith(content()).find(SECTION).exists()).toBe(false);
  });

  it('已经有边的旧图照样显示——否则那些边再也没有地方删', () => {
    const withEdge = content();
    withEdge.edges = [{ id: 'e1', source: 'a', target: 'b', label: '是', kind: 'branch' }];
    const wrapper = mountWith(withEdge);
    expect(wrapper.find(SECTION).exists()).toBe(true);
    // 列表里那条边真的在这个区块里：可见性与"能删"是同一件事。
    expect(wrapper.find(SECTION).text()).toContain('甲 → 乙');
    expect(wrapper.find(SECTION).text()).toContain('删除');
  });
});
