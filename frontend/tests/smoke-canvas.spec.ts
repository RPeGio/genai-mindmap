import { mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';

/**
 * 这个用例不 mock 画布：jsdom 里挂真实的 Vue Flow，确认组件树能建立。
 * 目的是挡住「store 没被 provide / 组件注册错」这类一挂就白的错误——
 * 用替身画布的界面用例发现不了它们。
 *
 * jsdom 没有 ResizeObserver，元素尺寸恒为 0，所以补两个最小垫片。
 */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
});

describe('画布冒烟', () => {
  it('真实 Vue Flow 能挂载出容器', () => {
    const wrapper = mount(App, {
      global: { plugins: [createPinia()] },
      attachTo: document.body,
    });

    expect(wrapper.find('.vue-flow').exists()).toBe(true);
    expect(wrapper.find('.vue-flow__background').exists()).toBe(true);
    wrapper.unmount();
  });
});
