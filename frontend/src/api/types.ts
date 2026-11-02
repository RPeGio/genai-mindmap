/** 后端契约 v1 的 TypeScript 镜像。字段以 backend/app/contracts/mindmap.py 为准；
 *  后端升到契约 v2 后由 openapi-typescript 生成 schema.d.ts 替换本文件。 */
export interface MindNode {
  id: string;
  label: string;
  children: MindNode[];
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
