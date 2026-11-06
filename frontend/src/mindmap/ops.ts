import type { MindMapContent, SourceRef } from '../api/types';

/**
 * 导图操作契约（前端侧）。
 *
 * 与 `backend/app/contracts/ops.py` 逐字段对应：字段名一律 snake_case，
 * 因为这些结构会原样跨端传输。不要在这里改成 camelCase —— 中间任何一次
 * 转换漏掉一个字段，表现都是"保存后引用没了"这种极难定位的问题。
 */

/** 挂一个新节点。`id` 由服务端规范化时分配，模型草案里用 `$tmp1` 这类临时名。 */
export interface AddOp {
  op: 'add';
  id: string;
  parent_id: string;
  index: number;
  label: string;
  source_refs?: SourceRef[];
}

/**
 * 改节点。
 *
 * `label` 与 `source_refs` 缺省（undefined）表示"这一项不动"，与"清空"区分开：
 * `source_refs: []` 是显式清空。这个区别丢不得。
 */
export interface UpdateOp {
  op: 'update';
  id: string;
  changes: { label?: string; source_refs?: SourceRef[] };
}

/** 删节点及整棵子树。根节点不可删。 */
export interface RemoveOp {
  op: 'remove';
  id: string;
}

/** 改变拓扑：把节点连同子树挂到新父节点下。唯一可能造环的 op。 */
export interface MoveOp {
  op: 'move';
  id: string;
  new_parent_id: string;
  index: number;
}

export type MindMapOp = AddOp | UpdateOp | RemoveOp | MoveOp;

/** 整批原子生效。执行器在 `ops.ts`，实现在同文件下方（待实现）。 */
export interface OpBatch {
  batch_id: string;
  base_revision: number;
  ops: MindMapOp[];
}

/** 单个 op 失败。`path` 形如 `ops.2.id`，会作为工具结果回给模型。 */
export class OpError extends Error {
  constructor(
    readonly code: string,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = 'OpError';
  }
}

export interface ApplyResult {
  content: MindMapContent;
  /** 被改动的节点 id，用于引用重校验与局部刷新。 */
  changed_ids: string[];
}
