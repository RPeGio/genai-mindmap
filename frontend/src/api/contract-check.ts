/**
 * 契约漂移的编译期防线。
 *
 * `./schema.d.ts` 由 `npm run gen:api` 从 `backend/openapi.json` 生成，那份快照又是
 * `backend/scripts/export_openapi.py` 从 `app.openapi()` 导出的。两条独立的检查守
 * 同一件事：
 *
 * - 本文件：镜像类型（`./types.ts`）与生成结果对得上，`vue-tsc` 期间就红；
 * - CI：重新生成 `schema.d.ts` 后 `git diff --exit-code`，快照过期也红。
 *
 * 断言分两类：
 *
 * 1. `SameKeys`——键集合必须一样。后端加了字段（比如 `Node.highlight`）而镜像没跟，
 *    或者镜像自造了后端不认识的字段，都会在这里编译失败。这一条是重点：历史上
 *    `source_refs` 与 `note` 都因为"镜像没声明"，在保存时被整批丢掉。
 * 2. `SameShape`——把可空性抹平之后，两边的字段类型要能互相赋值。`label` 从字符串
 *    改成对象、`children` 从数组改成 map，都会在这里炸；而"必填/可选"的差异不算，
 *    因为 pydantic 给 `default_factory` 字段（`children`、`failed_pages`）在 OpenAPI
 *    里标的是可选，实际响应里却总是有——镜像那边写成必填才是对的。
 *
 * 故意**不做**"镜像 → 后端 Input"的断言：后端的 Input 模式把带默认值的标量字段
 * （`collapsed`、`theme.preset`、`style.bold`）标成必填，可实际请求体不写这些字段
 * 也能通过校验（`extra="forbid"` 只拒绝多余字段，不要求缺省字段）。那条断言会一直
 * 红，红久了就没人看它了——一个常年红的检查等于没有检查。
 *
 * 本文件没有运行时副作用：`checks` 只被类型系统读取，构建时会被摇掉；它也不需要
 * 被任何模块 import，只要在 `tsconfig.json` 的 `include` 里就会被检查。
 *
 * 注意 `MindMapOp` / `OpBatch` 不在 OpenAPI 里（它们走 agent 的 SSE 事件，不是 HTTP
 * 请求体或响应体），那两个类型靠 `backend/tests/fixtures/mindmap_ops.json` 两端共用
 * 一份用例来对齐，见 `frontend/src/mindmap/ops.ts`。
 */
import type { components } from './schema';
import type {
  MapRecord,
  MindMapContent,
  MindMapTheme,
  MindNode,
  MindRelation,
  NodePosition,
  NodeStyle,
  Source,
  SourceRef,
  SourceSpan,
  SourceUsage,
} from './types';

type Schemas = components['schemas'];

/** 两个类型的键集合完全相同才通过。 */
type SameKeys<A, B> = [keyof A] extends [keyof B] ? ([keyof B] extends [keyof A] ? true : false) : false;

/** 把每一层字段都放宽成可选，只看"有哪些字段、字段是什么形状"。 */
type Deep<T> = T extends (infer Item)[]
  ? Deep<Item>[]
  : T extends object
    ? { [Key in keyof T]?: Deep<T[Key]> }
    : T;

/** 抹平可空性之后，两个形状能互相赋值才通过。 */
type SameShape<A, B> = Deep<A> extends Deep<B> ? (Deep<B> extends Deep<A> ? true : false) : false;

export const checks = {
  // ---- 键集合 ----
  contentKeys: true satisfies SameKeys<MindMapContent, Schemas['MindMapContent-Output']>,
  nodeKeys: true satisfies SameKeys<MindNode, Schemas['Node-Output']>,
  themeKeys: true satisfies SameKeys<MindMapTheme, Schemas['Theme']>,
  styleKeys: true satisfies SameKeys<NodeStyle, Schemas['NodeStyle']>,
  positionKeys: true satisfies SameKeys<NodePosition, Schemas['Position']>,
  relationKeys: true satisfies SameKeys<MindRelation, Schemas['Relation']>,
  sourceKeys: true satisfies SameKeys<Source, Schemas['Source']>,
  sourceRefKeys: true satisfies SameKeys<SourceRef, Schemas['SourceRef']>,
  sourceSpanKeys: true satisfies SameKeys<SourceSpan, Schemas['SourceSpan']>,
  sourceUsageKeys: true satisfies SameKeys<SourceUsage, Schemas['SourceUsage']>,
  recordKeys: true satisfies SameKeys<MapRecord, Schemas['MapRecord']>,

  // ---- 字段形状 ----
  contentShape: true satisfies SameShape<MindMapContent, Schemas['MindMapContent-Output']>,
  nodeShape: true satisfies SameShape<MindNode, Schemas['Node-Output']>,
  themeShape: true satisfies SameShape<MindMapTheme, Schemas['Theme']>,
  styleShape: true satisfies SameShape<NodeStyle, Schemas['NodeStyle']>,
  positionShape: true satisfies SameShape<NodePosition, Schemas['Position']>,
  relationShape: true satisfies SameShape<MindRelation, Schemas['Relation']>,
  sourceShape: true satisfies SameShape<Source, Schemas['Source']>,
  sourceRefShape: true satisfies SameShape<SourceRef, Schemas['SourceRef']>,
  sourceSpanShape: true satisfies SameShape<SourceSpan, Schemas['SourceSpan']>,
  sourceUsageShape: true satisfies SameShape<SourceUsage, Schemas['SourceUsage']>,
  recordShape: true satisfies SameShape<MapRecord, Schemas['MapRecord']>,
};
