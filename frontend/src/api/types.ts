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

/** 已保存的导图记录。`version` 是乐观锁的版本号，保存时要原样带回去。 */
export interface MapRecord {
  id: string;
  version: number;
  content: MindMapContent;
  updated_at: string;
}

// ---- 来源（文档解析）----

/** 来源模态。后端 SourceKind 的镜像，目前只有 document 走得通。 */
export type SourceKind = 'text' | 'markdown' | 'document' | 'image' | 'audio';

/** 来源定位，供节点溯源。 */
export interface SourceSpan {
  /** 整份文档共几页（PDF 才有），不是"解析了几页"。 */
  page: number | null;
  start: number | null;
  end: number | null;
}

/**
 * 解析后的统一来源。字段与 backend/app/contracts/source.py 的 `Source` 一一对应：
 * 后端是 `extra="forbid"`，多一个字段就 422，所以这里宁可保守也不要自作主张加字段。
 */
export interface Source {
  id: string;
  kind: SourceKind;
  filename: string | null;
  /** 抽取出的正文；解析失败时为 null。 */
  text: string | null;
  span: SourceSpan | null;
  status: 'pending' | 'parsed' | 'failed';
  error: string | null;
  /** 实际得到内容的页数：51 页的 PDF 只处理前 50 页时这里是 50，span.page 才是 51。 */
  parsed_pages: number | null;
  /** 是否因后端页数上限被截断（后半截根本没处理）。 */
  truncated: boolean;
  /**
   * 处理过但彻底没拿到内容的页码（1 起）。
   *
   * 与 truncated 是两回事：那个是"没处理到"，这个是"处理了但中间有洞"。
   * 合并成一个布尔值会给出"共 3 页，仅处理前 3 页"这种自相矛盾的提示。
   */
  failed_pages: number[];
}

export interface CreateTextSourceResponse {
  source: Source;
}

/** 生成流水线的响应。stages / diagnostics 目前只用于排障，界面不渲染。 */
export interface PipelineResponse {
  mode: 'demo' | 'model';
  content: MindMapContent;
  diagnostics: unknown[];
  stages: unknown[];
}
