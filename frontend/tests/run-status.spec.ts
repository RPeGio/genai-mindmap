import { describe, expect, it } from 'vitest';

import {
  UNKNOWN_MODEL,
  agentDetailVisible,
  diagnosticCount,
  diagnosticsHint,
  hasModelInfo,
  modelTraceLine,
  runResultLine,
} from '../src/mindmap/run-status';

/**
 * 这两组判据都有**两个渲染点**，所以它们只能各有一份实现：
 *
 * - 模型名：消息流里的那枚小标签（`MessageFab`）；
 * - 诊断数量：状态行那句话 + 明细按钮上的字（`ChatDock`）、抽屉里的标题（`AgentPanel`）、
 *   以及对话框淡出之后还看得见的 `store.status`。
 *
 * 两处各写一遍的后果不是"不好看"，是一边显示"有 2 条"、另一边是空的——用户会以为
 * 界面坏了。所以判据留在这里，两个渲染点只负责画。
 */

describe('模型名：请求的那个与服务回报的那个', () => {
  it('两个都写出来：服务端把请求路由到别处要看得出', () => {
    expect(modelTraceLine({ requested: 'deepseek-chat', served: 'deepseek-flash' }))
      .toBe('请求 deepseek-chat → 服务 deepseek-flash');
  });

  it('服务端没回报时写"未知"，不拿请求名顶替', () => {
    expect(modelTraceLine({ requested: 'deepseek-chat' }))
      .toBe('请求 deepseek-chat → 服务 未知');
    expect(modelTraceLine({ requested: 'deepseek-chat', served: null }))
      .toBe('请求 deepseek-chat → 服务 未知');
    expect(modelTraceLine({ requested: 'deepseek-chat', served: '   ' }))
      .toBe('请求 deepseek-chat → 服务 未知');
  });

  it('请求名也没说时同样写"未知"（不编一个 deepseek-chat）', () => {
    expect(UNKNOWN_MODEL).toBe('未知');
    expect(modelTraceLine({})).toBe('请求 未知 → 服务 未知');
  });

  it('两条都没有内容时整块不显示（别挂一条"未知 → 未知"）', () => {
    expect(hasModelInfo({})).toBe(false);
    expect(hasModelInfo({ requested: null, served: null })).toBe(false);
    expect(hasModelInfo({ served: 'deepseek-flash' })).toBe(true);
    expect(hasModelInfo({ requested: 'deepseek-chat' })).toBe(true);
  });
});

describe('诊断数量', () => {
  const warnings = [
    { kind: 'diagnostic' },
    { kind: 'partial' },
    { kind: 'diagnostic' },
  ];

  it('只数 diagnostic：partial 有自己那句话说', () => {
    expect(diagnosticCount(warnings)).toBe(2);
    expect(diagnosticCount([])).toBe(0);
    expect(diagnosticsHint(0)).toBe('');
  });

  it('那半句带着数字，能与按钮上的字对上', () => {
    expect(diagnosticsHint(diagnosticCount(warnings))).toBe('有 2 条诊断');
  });
});

describe('落地那一行', () => {
  it('整张新图时不说"改了 0 处"', () => {
    expect(runResultLine({ nodes: 12, changes: null })).toContain('整张新图');
    expect(runResultLine({ nodes: 12, changes: 0 })).toContain('一处都没改');
    expect(runResultLine({ nodes: 12, changes: 3 })).toContain('改了 3 处');
  });
});

describe('「这次生成有 N 条诊断」也算有明细', () => {
  const base = {
    agentRunning: false,
    generatedSummary: null,
    conflictDraft: null,
    agentEvents: [],
    agentError: '',
  };

  it('有诊断、但别的信号都没有时，明细仍然要在场', () => {
    // 状态行上写着「有 2 条诊断」，而这块不出现 → 那句话点开是空的。
    // 生成成功那条路上 `generatedSummary` 恰好非空，所以这个空窗只在别的路径露头，
    // 但判据不该依赖"另一件事恰好成立"。
    expect(agentDetailVisible(base)).toBe(false);
    expect(agentDetailVisible({ ...base, agentWarnings: [{ length: 2 }] })).toBe(true);
    expect(agentDetailVisible({ ...base, agentWarnings: [] })).toBe(false);
  });
});
