import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActiveRecording } from '../src/audio/recorder';

/**
 * 「按住说话」的时序。
 *
 * 这个按钮只有一个手势，但它是异步的：`startRecording()` 要等 `getUserMedia` 的
 * 授权框，那一步可能几百毫秒到几秒。**用户的手不会等它**——松手发生在授权之前是
 * 完全正常的一次操作，而旧写法把它当成了"还没开始，什么都不用做"：等麦克风真的
 * 打开，组件照常进入录音态，用户明明松手了却还在录。
 *
 * 这组用例用一个"悬着的" `startRecording` 把那一刻冻住，再分别验证两种收场：
 * 松手（应当取消这一段）与卸载（应当取消并复位全局录音标记）。
 */

function fakeRecording() {
  const file = new File([new Uint8Array([1, 2, 3])], 'voice.wav', { type: 'audio/wav' });
  const stop = vi.fn<() => Promise<File>>(async () => file);
  const cancel = vi.fn<() => void>();
  const recording: ActiveRecording = { elapsedSeconds: () => 0, stop, cancel };
  return { recording, stop, cancel };
}

/** `emitted()` 的最后一条：`Array.prototype.at` 不在本仓库的 TS lib 里。 */
function lastEmitted(events: unknown[][] | undefined): unknown[] | undefined {
  return events && events.length ? events[events.length - 1] : undefined;
}

interface Deferred {
  startRecording: ReturnType<typeof vi.fn>;
  resolve: (value: ActiveRecording) => void;
}

/** 把 `startRecording` 换成一个永远悬着的 Promise，返回它和"授权回来"的开关。 */
async function installHangingRecorder(): Promise<{ state: Deferred; VoicePress: typeof import('../src/components/VoicePress.vue').default }> {
  const state: Deferred = { startRecording: vi.fn(), resolve: () => {} };
  state.startRecording.mockImplementation(() => new Promise<ActiveRecording>(resolve => {
    state.resolve = resolve;
  }));
  vi.doMock('../src/audio/recorder', async () => {
    const actual = await vi.importActual<typeof import('../src/audio/recorder')>('../src/audio/recorder');
    return { ...actual, startRecording: state.startRecording, RecordingError: actual.RecordingError };
  });
  vi.resetModules();
  const { default: VoicePress } = await import('../src/components/VoicePress.vue');
  return { state, VoicePress };
}

afterEach(() => {
  vi.doUnmock('../src/audio/recorder');
  vi.resetModules();
});

describe('按住说话的时序', () => {
  it('授权还没回来就松手：授权回来后取消，不进入录音态', async () => {
    const { state, VoicePress } = await installHangingRecorder();
    const wrapper = mount(VoicePress);

    await wrapper.get('[data-test="press-to-talk"]').trigger('pointerdown');
    expect(state.startRecording).toHaveBeenCalledTimes(1);
    expect(wrapper.get('[data-test="press-to-talk"]').text()).toContain('正在启动麦克风');

    // 手松了，麦克风还没开
    window.dispatchEvent(new Event('pointerup'));
    await flushPromises();

    const recording = fakeRecording();
    state.resolve(recording.recording);
    await flushPromises();

    expect(recording.cancel).toHaveBeenCalledTimes(1);
    expect(recording.stop).not.toHaveBeenCalled();
    expect(wrapper.get('[data-test="press-to-talk"]').text()).toContain('按住说话');
    // 松手也要把"正在录音"的标志放掉，否则生成按钮会被它锁住。
    expect(lastEmitted(wrapper.emitted('active'))).toEqual([false]);
    wrapper.unmount();
  });

  it('授权期间切回键盘（组件卸载）：取消并复位全局录音标记', async () => {
    const { state, VoicePress } = await installHangingRecorder();
    const wrapper = mount(VoicePress);

    await wrapper.get('[data-test="press-to-talk"]').trigger('pointerdown');
    // 按下那一刻已经报过 active=true
    expect(wrapper.emitted('active')?.[0]).toEqual([true]);

    // `v-if="voiceMode"` 切回键盘：组件整个卸载，而麦克风还悬在授权上
    wrapper.unmount();
    const recording = fakeRecording();
    state.resolve(recording.recording);
    await flushPromises();

    expect(recording.cancel).toHaveBeenCalledTimes(1);
    expect(recording.stop).not.toHaveBeenCalled();
    // 最后一条必须是 false：`sources.recording` 由这个事件驱动，漏了它用户
    // 切一次键盘就再也生成不了，而界面上只说"正在录音"。
    expect(lastEmitted(wrapper.emitted('active'))).toEqual([false]);
  });

  it('按住不松手、授权回来后正常进入录音态，松手才 stop', async () => {
    const { state, VoicePress } = await installHangingRecorder();
    const wrapper = mount(VoicePress);

    await wrapper.get('[data-test="press-to-talk"]').trigger('pointerdown');
    const recording = fakeRecording();
    state.resolve(recording.recording);
    await flushPromises();

    expect(recording.cancel).not.toHaveBeenCalled();
    expect(wrapper.get('[data-test="press-to-talk"]').text()).toContain('松开');

    window.dispatchEvent(new Event('pointerup'));
    await flushPromises();
    expect(recording.stop).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('recorded')).toHaveLength(1);
    expect(lastEmitted(wrapper.emitted('active'))).toEqual([false]);
    wrapper.unmount();
  });
});
