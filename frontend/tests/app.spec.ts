import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';

/** client 只读 ok/status/json，这里用最小对象代替 Response，避免依赖 jsdom 是否实现 fetch。 */
function reply(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('空输入时不发请求，并提示用户', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(App);

    await wrapper.get('button').trigger('click');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(wrapper.get('[role="status"]').text()).toContain('请输入主题或文本');
  });

  it('生成成功后按层级展开节点', async () => {
    const payload = {
      mode: 'demo',
      content: {
        title: '机器学习',
        root: { id: 'r', label: '机器学习', children: [{ id: 'c', label: '监督学习', children: [] }] },
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply(payload)),
    );
    const wrapper = mount(App);

    await wrapper.get('textarea').setValue('机器学习');
    await wrapper.get('button').trigger('click');
    await vi.waitFor(() => expect(wrapper.findAll('li')).toHaveLength(2));

    expect(wrapper.text()).toContain('演示模式');
    expect(wrapper.text()).toContain('监督学习');
  });

  it('后端返回非 2xx 时展示 detail 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply({ detail: '模型调用失败' }, 502)),
    );
    const wrapper = mount(App);

    await wrapper.get('textarea').setValue('x');
    await wrapper.get('button').trigger('click');
    await vi.waitFor(() => expect(wrapper.get('[role="status"]').text()).toContain('模型调用失败'));
  });
});
