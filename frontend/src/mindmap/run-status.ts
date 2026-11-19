/**
 * 一次生成落地之后，界面上要说清的三件事：落了多大、改了几处、有没有可比基线。
 *
 * 这句文案现在有**两个渲染点**：画布底部那条常驻状态行，与抽屉里的「本次生成明细」
 * （`AgentPanel`）。同一件事在两处各写一遍，改文案时必然漏一处，用户就会看到两条
 * 互相矛盾的说法——所以判据只留在这里。
 */
export interface RunSummary {
  nodes: number;
  changes: number | null;
}

export function runResultLine(summary: RunSummary): string {
  const shape = `已落到画布 · ${summary.nodes} 个节点`;
  // `changes === null` 表示画布本来是空的：这时说"改了 0 处"是误导，整棵树都是新的。
  if (summary.changes === null) return `${shape}（整张新图）`;
  return summary.changes === 0 ? `${shape} · 一处都没改` : `${shape} · 改了 ${summary.changes} 处`;
}

/**
 * 「模型协作」那一块该不该出现。
 *
 * 抽屉里的明细与状态行上的「明细」按钮必须同时在场：一边显示"有明细"、另一边却是
 * 空的，是最容易让人以为界面坏了的形状。判据同理只留一处。
 */
export interface AgentVisibilityState {
  agentRunning: boolean;
  generatedSummary: unknown;
  conflictDraft: unknown;
  agentEvents: { length: number };
  agentError: string;
}

export function agentDetailVisible(state: AgentVisibilityState): boolean {
  return state.agentRunning
    || state.generatedSummary !== null
    || state.conflictDraft !== null
    || state.agentEvents.length > 0
    || !!state.agentError;
}
