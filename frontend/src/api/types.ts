/** 后端契约 v1 的 TypeScript 镜像。字段以 backend/app/contracts/mindmap.py 为准；
 *  后端升到契约 v2 后由 openapi-typescript 生成 schema.d.ts 替换本文件。 */
export interface MindNode {
  id: string;
  /** 一句话标题。画布上按 24 字截断显示，全文在悬停卡片与详情面板里。 */
  label: string;
  children: MindNode[];
  /**
   * 展开正文（契约 v2 字段，v1 后端不返回）。
   *
   * 语义：`label` 是短标题、`note` 承载正文；渲染时用 `note ?? label` 回退，
   * 因此 v1 阶段画布/详情面板显示的仍是 `label` 全文，v2 落地后自动切换。
   * 必须与契约 v2 同批上线：v1 的 `Node` 是 `extra="forbid"`，提前出现会 422。
   */
  note?: string | null;
}

export interface MindMapContent {
  title: string;
  root: MindNode;
}

/** 同步生成接口的响应：mode 说明内容来自演示规则还是模型。 */
export interface GenerateResponse {
  mode: 'demo' | 'model';
  content: MindMapContent;
}
