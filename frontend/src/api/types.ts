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
  /**
   * 节点级引用，镜像后端 `Node.source_refs`（v1 契约里**已经存在**，不是 v2 预告）。
   *
   * 之前前端缺这个字段，`client.ts::toContractV1()` 只保留 id/label/children，
   * 于是引用在保存时被整批删掉——刷新后永远看不到引用，跟渲染层无关。
   * 字符区间以 `Source.text` 为准，用 Unicode 码点计（不是 UTF-16 下标）。
   */
  source_refs?: SourceRef[];
}

/** 节点引用。与 backend/app/contracts/source.py 的 `SourceRef` 对应。 */
export interface SourceRef {
  source_id: string;
  /** 页码，1 起；非分页来源为 null。 */
  page?: number | null;
  /** 字符区间 [start, end)，按码点计；两项必须同时给出。 */
  start?: number | null;
  end?: number | null;
  /** 逐字摘录的原文，≤300 字。 */
  quote?: string | null;
}

// ---- 导图操作契约（与 backend/app/contracts/ops.py 逐字段对应）----
//
// 放在 types.ts 而不是 mindmap/ops.ts：agent 事件流要引用 OpBatch，而
// ops.ts 已经反过来依赖本文件，再让本文件依赖它就成了循环。契约归契约，
// 执行器归执行器。字段一律 snake_case，这些结构会原样跨端传输。

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

/** 整批原子生效。 */
export interface OpBatch {
  batch_id: string;
  base_revision: number;
  ops: MindMapOp[];
}

export interface MindMapContent {
  title: string;
  root: MindNode;
  /** 整图来源摘要；旧导图可以省略，不代表节点级引用。 */
  source_usage?: SourceUsage[];
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

/** 来源模态，镜像后端 SourceKind。 */
export type SourceKind = 'text' | 'markdown' | 'document' | 'image' | 'audio';

export interface SourceUsage {
  source_id: string;
  kind: SourceKind;
  filename: string | null;
  char_count: number;
}

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
  /** 音频时长（秒）；兼容未返回此字段的旧响应。 */
  duration_seconds?: number | null;
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

// ---- agent 运行 ----

/**
 * 一次 agent 运行。字段与 backend/app/agent/contracts.py 的 `AgentRunRequest` 对应。
 *
 * `create` 不能带 snapshot、`expand`/`edit` 必须带——那条约束在后端开流前校验，
 * 这里不重复，因为报错文案由后端统一给。
 */
export interface AgentRunRequest {
  goal: string;
  intent: 'create' | 'expand' | 'edit';
  /**
   * 拆解方式：同一句主题换一套步骤，长出来的树结构就不同。
   *
   * 取值与后端 `generation.service.THINKING_MODELS` 的键一一对应；后端是
   * Literal，拼错会被 422 拒（不是静默退化成 default），所以这里保持宽松的
   * string，由下拉框保证只在五个值里出现。缺省即 `default`。
   */
  thinking_model?: string;
  snapshot?: MindMapContent | null;
  selected_ids?: string[];
  /**
   * 本次请求附带的来源，镜像后端 `AgentRunRequest.sources`（max_length=50）。
   *
   * 材料正文走这里，不拼进 goal：拼进去会被当成"用户的主题"，
   * 而模型需要的是带 source_id 的结构化数据——否则它写不出合法的引用。
   */
  sources?: Source[];
}

/**
 * agent 事件流。`seq` 从 1 连续递增，前端靠它判断丢帧与乱序。
 *
 * 与旧流式生成的 `StreamEvent` 分开：后者是「边生成边吐节点」，这里是
 * 「边跑边报工具进度」，两者的状态机完全不同，硬塞进一个联合类型会让
 * store 里的 switch 变成一团。
 */
export type AgentEvent =
  | { type: 'stage'; seq: number; stage: string; detail?: string }
  | { type: 'tool_start'; seq: number; call_id: string; name: string; args_summary?: Record<string, unknown> }
  | {
      type: 'tool_end';
      seq: number;
      call_id: string;
      name: string;
      ok: boolean;
      result_summary?: Record<string, unknown>;
      latency_ms?: number;
    }
  | { type: 'ops'; seq: number; call_id: string; batch: OpBatch }
  | { type: 'snapshot'; seq: number; content: MindMapContent }
  | { type: 'done'; seq: number; content?: MindMapContent | null; usage?: Record<string, unknown> }
  | { type: 'error'; seq: number; code: string; message: string; limit?: string };
