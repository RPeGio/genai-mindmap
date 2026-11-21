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
  /**
   * 「这次生成有 N 条诊断」——**它也算有明细**。
   *
   * 漏掉它的后果是一个具体的空窗：`diagnosticCount(agentWarnings) > 0` 时状态行上
   * 会写着「有 2 条诊断」，而这一块如果不出现，那句数字点开之后什么都没有。生成
   * 成功那条路上 `generatedSummary` 恰好非空，所以漏了也看不出来——它是一条只在
   * 别的路径上才露头的判据，而判据本身不该依赖"另一件事恰好成立"。
   */
  agentWarnings?: { length: number };
}

export function agentDetailVisible(state: AgentVisibilityState): boolean {
  return state.agentRunning
    || state.generatedSummary !== null
    || state.conflictDraft !== null
    || state.agentEvents.length > 0
    || !!state.agentError
    || (state.agentWarnings?.length ?? 0) > 0;
}

/**
 * 模型名该怎么写。**请求名字与服务名字是两件事，两个都要显示。**
 *
 * 实测这个 API 接受 `deepseek-chat` 并把请求路由到 `deepseek-flash`（两个名字是同一
 * 个模型的两个别名，区别只在思不思考）。只写请求名，界面上永远看不出"服务端把我
 * 的请求转给了别处"——而这件事我们整整一周都不知道。
 *
 * 缺值写"未知"而不是拿请求名顶替：`served_model` 缺字段意味着对方没说，
 * 把请求名填进去就是把"没说"变成"说了"。
 */
export const UNKNOWN_MODEL = '未知';

export interface ModelTrace {
  /** 这次请求里写出去的模型名（`done` 帧的 `model`）。 */
  requested?: string | null;
  /** 响应体里回报的模型名（`done` 帧的 `served_model`）。 */
  served?: string | null;
}

export function modelTraceLine(trace: ModelTrace): string {
  const requested = trace.requested?.trim() || UNKNOWN_MODEL;
  const served = trace.served?.trim() || UNKNOWN_MODEL;
  return `请求 ${requested} → 服务 ${served}`;
}

/** 这两条里有没有任何一条能说：都说不出就整块不显示，免得挂一条"未知 → 未知"。 */
export function hasModelInfo(trace: ModelTrace): boolean {
  return !!(trace.requested?.trim() || trace.served?.trim());
}

/**
 * 诊断（不含 `partial` 那一类）有几条。
 *
 * 状态行与抽屉里的入口都要说这个数：原来只写「有诊断提示」，用户在界面上找了半天
 * 找不到"诊断"在哪——一句**有数量**的提示才是可核对的。
 */
export function diagnosticCount(warnings: readonly { kind: string }[]): number {
  return warnings.filter(item => item.kind === 'diagnostic').length;
}

/** 状态行/落地消息里的那半句：`有 2 条诊断`；没有诊断时是空串。 */
export function diagnosticsHint(count: number): string {
  return count > 0 ? `有 ${count} 条诊断` : '';
}
