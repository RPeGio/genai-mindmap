import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActiveRecording } from '../src/audio/recorder';

/**
 * `RecordButton`（材料面板里那个"录音添加来源"）的退出路径。
 *
 * 它与 `VoicePress` 是同一件事的两个组件、同一份 `sources.recording` 标志，
 * 所以"卸载时复位标志"这条规矩必须两处都有。这一条来自一次真实的可达路径：
 *
 *   1. 在材料面板里开始录音（`sources.recording = true`，生成按钮因此禁用）；
 *   2. 选中一个节点、点工具条的「展开」——`expandSelected` **不**检查 recording，
 *      于是生成开始；
 *   3. 生成态把整块对话框（连同材料面板与这个组件）卸载；
 *   4. 组件没复位标志 → `sources.recording` 永远为 true → 之后再也提交不了，
 *      界面上只说"正在录音"。
 */
async function installHangingRecorder() {
  let resolve: (value: ActiveRecording) => void = () => {};
  const startRecording = vi.fn(() => new Promise<ActiveRecording>(done => {
    resolve = done;
  }));
  vi.doMock('../src/audio/recorder', async () => {
    const actual = await vi.importActual<typeof import('../src/audio/recorder')>('../src/audio/recorder');
    return { ...actual, startRecording, RecordingError: actual.RecordingError };
  });
  vi.resetModules();
  const { default: RecordButton } = await import('../src/components/RecordButton.vue');
  return { RecordButton, resolve: (value: ActiveRecording) => resolve(value) };
}

function fakeRecording(): ActiveRecording {
  return {
    elapsedSeconds: () => 0,
    stop: vi.fn<() => Promise<File>>(async () => new File([new Uint8Array([1])], 'v.wav')),
    cancel: vi.fn<() => void>(),
  };
}

afterEach(() => {
  vi.doUnmock('../src/audio/recorder');
  vi.resetModules();
});

describe('RecordButton 的卸载', () => {
  it('录音中卸载：取消录音并复位全局录音标志', async () => {
    const { RecordButton, resolve } = await installHangingRecorder();
    const wrapper = mount(RecordButton);

    await wrapper.get('button').trigger('click');
    expect(wrapper.emitted('active')?.[0]).toEqual([true]);

    wrapper.unmount();
    const recording = fakeRecording();
    resolve(recording);
    await flushPromises();

    expect(recording.cancel).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('active')?.[wrapper.emitted('active')!.length - 1]).toEqual([false]);
  });
});
