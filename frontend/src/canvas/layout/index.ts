import type { DiagramType, MindEdge, MindNode } from '../../api/types';
import { layoutFishbone, type FishboneGeometry } from './fishbone';
import { layoutFlow } from './flow';
import { layoutTidy, type Direction, type LayoutResult } from './tidy';

/**
 * 图型 → 布局的分派点。
 *
 * 为什么分派落在**布局层**而不是 `MindMapCanvas.vue`：
 *
 * 画布组件吃的是 store 里已经算好的 `nodes` / `edges`（扁平节点 + 连线），它手上
 * 没有任何一棵可以重排的树；而连线的手柄（`out-left` / `in-top`…）是**按坐标**
 * 反推的（`childSide`），节点一换算法，连线必须跟着换。分派若写在组件里，坐标会
 * 与连线分家：组件按鱼骨摆节点，store 里的 `edges` 仍在按树布局的坐标挑句柄。
 *
 * 所以分派只有一处：store 的 `layout` computed 调本函数，`nodes` 与 `edges` 都从
 * 同一份结果里读。组件那边仍然只做一件事——按 `type` 把连线交给对应的渲染器
 * （`smoothstep` / `mindEdge` / `fishbone`）。
 *
 * 三种图型共用 `LayoutResult` 这个出参形状（`positions` 是**节点中心**），额外的
 * 几何（流程图的回退边、鱼骨图的骨线）作为可选字段挂在同一份结果上，谁需要谁读。
 */
export interface DiagramLayout extends LayoutResult {
  /** 流程图才有：目标层号 ≤ 源层号的附加边 id。 */
  backEdges?: ReadonlySet<string>;
  /** 鱼骨图才有：主骨与斜骨上的挂点（`父id->子id` → 折线）。 */
  fishbone?: FishboneGeometry;
}

export interface DiagramInput {
  root: MindNode;
  /** 画布上的附加边（`MindMapContent.edges`）。树布局不看它。 */
  edges?: readonly MindEdge[];
  /** `content.diagram_type`；缺省（旧图）按 `tree`。 */
  diagramType?: DiagramType;
  /** 树布局的朝向。鱼骨与流程图的几何是固定的，忽略它（见各自文件头）。 */
  direction?: Direction;
}

/**
 * 这张图是什么图型。
 *
 * `?? 'tree'` 只能出现在这一处：`diagram_type` 是后加的字段，库里所有旧图都没有
 * 它，而它们全部由思维导图那条提示词生成。写成 `=== 'tree'` 判断会把旧图判成
 * "未知图型"，落到某个 fallback 分支上——那正是"打开旧图，画布变了"的来源。
 */
export function diagramTypeOf(content: { diagram_type?: DiagramType } | null | undefined): DiagramType {
  return content?.diagram_type ?? 'tree';
}

/**
 * 按图型算布局。
 *
 * `tree` 这条分支必须**逐像素**等于 `layoutTidy(root, { direction })`：树布局有
 * 大量用例钉着坐标，任何参数换值、任何中间层归一都会让它们一起变。所以这里对它
 * 不做任何加工，原样透传（`direction` 没给就什么都不传，与旧调用的入参一字不差）。
 */
export function layoutDiagram(input: DiagramInput): DiagramLayout {
  const { root, edges = [], diagramType = 'tree', direction } = input;

  if (diagramType === 'fishbone') return layoutFishbone(root);
  if (diagramType === 'flowchart') return layoutFlow(root, edges);
  return layoutTidy(root, direction === undefined ? {} : { direction });
}
