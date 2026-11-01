// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../src/App';
import { api } from '../src/api';
import type { MindMapContent, MindMapRecord } from '../src/types';

vi.mock('../src/api', () => ({ api: {
  list: vi.fn(), get: vi.fn(), generate: vi.fn(), create: vi.fn(), update: vi.fn(),
} }));

const history: MindMapRecord = {
  id: 'history', version: 1, updated_at: '2026-01-01T00:00:00Z',
  content: { title: '历史导图', root: { id: 'h', label: '历史根节点', children: [] }, source_usage: [] },
};
const draft: MindMapContent = { title: '新导图', root: { id: 'r', label: '新建根节点', children: [] }, source_usage: [] };

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.list).mockResolvedValue([history]);
  vi.mocked(api.get).mockResolvedValue(history);
  vi.mocked(api.generate).mockResolvedValue({ mode: 'demo', content: draft });
  vi.mocked(api.create).mockImplementation(async content => ({ ...history, id: 'created', content }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function makeDraft() {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('button', { name: '历史导图' });
  await user.type(screen.getByLabelText('主题或文本'), '新主题');
  await user.click(screen.getByRole('button', { name: '生成导图' }));
  await screen.findByDisplayValue('新建根节点');
  return user;
}

describe('未保存内容保护', () => {
  it('取消切换或重新生成时保留编辑，离开页面时提示', async () => {
    const user = await makeDraft();
    const node = screen.getByLabelText('节点名称') as HTMLInputElement;
    await user.clear(node);
    await user.type(node, '修改后的节点');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: '历史导图' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(api.get).not.toHaveBeenCalled();
    expect(node.value).toBe('修改后的节点');
    await user.click(screen.getByRole('button', { name: '生成导图' }));
    expect(api.generate).toHaveBeenCalledTimes(1);
    expect(node.value).toBe('修改后的节点');
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(false);
  });

  it('保存成功后可直接切换，保存期间禁止修改', async () => {
    let complete!: (value: MindMapRecord) => void;
    vi.mocked(api.create).mockImplementation(content => new Promise(resolve => {
      complete = () => resolve({ ...history, id: 'created', content });
    }));
    const user = await makeDraft();
    await user.click(screen.getByRole('button', { name: '保存' }));
    expect((screen.getByLabelText('节点名称').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
    complete(history);
    await screen.findByText('所有修改已保存');
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(true);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: '历史导图' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('history'));
    expect(confirm).not.toHaveBeenCalled();
    expect((screen.getByLabelText('节点名称') as HTMLInputElement).value).toBe('历史根节点');
  });

  it('模型请求失败时保留现有草稿', async () => {
    const user = await makeDraft();
    const node = screen.getByLabelText('节点名称') as HTMLInputElement;
    await user.clear(node);
    await user.type(node, '保留我');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(api.generate).mockRejectedValueOnce(new Error('模型不可用'));
    await user.click(screen.getByRole('button', { name: '生成导图' }));
    await screen.findByText('模型不可用');
    expect(node.value).toBe('保留我');
    expect(screen.getByText('有未保存的修改')).toBeTruthy();
  });
});
