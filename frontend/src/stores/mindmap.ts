import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, generateMindMapStream } from '../api/client';
import type { MindMapContent, MindNode } from '../api/types';
import { layoutTidy, handleSide, NODE_SIZE, type Direction, type LayoutResult } from '../canvas/layout/tidy';

/** 画布节点上最多显示的字数（按码点算，避免把 emoji 切成半个）。 */
export const TRUNCATE_CHARS = 24;

/** 节点上显示的短标题：按码点截断，超出加省略号。 */
export function displayTitle(node: MindNode): string {
  const chars = Array.from(node.label.trim());
  return chars.length > TRUNCATE_CHARS ? `${chars.slice(0, TRUNCATE_CHARS).join('')}…` : chars.join('');
}

/**
 * 卡片与详情面板里的正文：契约 v2 的 `note` 优先，v1 没有该字段时回退 `label` 全文。
 * v2 落地后这里不需要改调用方，只改这一个函数的取值。
 */
export function displayBody(node: MindNode): string {
  const note = node.note?.trim();
  return note && note.length > 0 ? note : node.label.trim();
}

export interface MindNodeData {
  /** 截断后的短标题，画在节点框里 */
  label: string;
  /** 悬停卡片与详情面板里的全文 */
  body: string;
  /** 完整 label（不截断），供 title 属性与无障碍读屏使用 */
  fullLabel: string;
  depth: number;
  /** 句柄挂哪一侧：水平布局左右、垂直布局顶部 */
  side: 'left' | 'right' | 'top';
}

export interface FlatNode {
  node: MindNode;
  depth: number;
  parentId: string | null;
}

function flatten(root: MindNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: MindNode, depth: number, parentId: string | null) => {
    out.push({ node, depth, parentId });
    for (const child of node.children) walk(child, depth + 1, node.id);
  };
  walk(root, 0, null);
  return out;
}

export const useMindMapStore = defineStore('mindmap', () => {
  const content = ref<MindMapContent | null>(null);
  const mode = ref<'demo' | 'model' | null>(null);
  const status = ref('输入主题或文本后生成，节点会边生成边出现。');
  const stageText = ref('');
  const waitedMs = ref(0);
  const busy = ref(false);
  const selectedId = ref<string | null>(null);
  const direction = ref<Direction>('right');
  /** 用户手动平移/缩放后不再自动 fitView，否则每次节点增长都会把视图拽回去。 */
  const viewAdjusted = ref(false);

  let controller: AbortController | null = null;

  const layout = computed<LayoutResult | null>(() =>
    content.value ? layoutTidy(content.value.root, { direction: direction.value }) : null,
  );

  const flatNodes = computed<FlatNode[]>(() =>
    content.value ? flatten(content.value.root) : [],
  );

  const nodes = computed<FlowNode<MindNodeData>[]>(() => {
    const current = layout.value;
    if (!current) return [];
    return flatNodes.value.map(({ node, depth }) => {
      const point = current.positions.get(node.id);
      return {
        id: node.id,
        type: 'mind',
        draggable: false,
        connectable: false,
        position: {
          x: (point?.x ?? 0) - NODE_SIZE.width / 2,
          y: (point?.y ?? 0) - NODE_SIZE.height / 2,
        },
        data: {
          label: displayTitle(node),
          body: displayBody(node),
          fullLabel: node.label,
          depth,
          side: handleSide(node.id, current),
        },
      } satisfies FlowNode<MindNodeData>;
    });
  });

  const edges = computed<FlowEdge[]>(() => {
    const out: FlowEdge[] = [];
    for (const { node, parentId } of flatNodes.value) {
      if (!parentId) continue;
      out.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        sourceHandle: 'out',
        target: node.id,
        targetHandle: 'in',
        type: 'smoothstep',
      });
    }
    return out;
  });

  const nodeCount = computed(() => flatNodes.value.length);

  const selectedNode = computed<FlatNode | null>(
    () => flatNodes.value.find(entry => entry.node.id === selectedId.value) ?? null,
  );

  /** 从根到当前节点的路径，详情面板用来标明"这句话出自哪一支"。 */
  const breadcrumb = computed<MindNode[]>(() => {
    const target = selectedId.value;
    if (!content.value || !target) return [];
    const path: MindNode[] = [];
    const walk = (node: MindNode): boolean => {
      path.push(node);
      if (node.id === target) return true;
      for (const child of node.children) {
        if (walk(child)) return true;
      }
      path.pop();
      return false;
    };
    return walk(content.value.root) ? path : [];
  });

  /**
   * 把增量节点合并进当前树。
   * 同一个 id 会反复到达（文字逐渐变长），所以已存在时更新文字、保留其子节点。
   */
  function upsert(node: MindNode, parentId: string, incoming: MindNode): MindNode {
    if (node.id === parentId) {
      const index = node.children.findIndex(child => child.id === incoming.id);
      const children =
        index >= 0
          ? node.children.map((child, i) =>
              i === index ? { ...incoming, children: child.children } : child,
            )
          : [...node.children, incoming];
      return { ...node, children };
    }
    return { ...node, children: node.children.map(child => upsert(child, parentId, incoming)) };
  }

  function applyNode(parentId: string | null, incoming: MindNode) {
    if (!content.value) {
      content.value = { title: incoming.label, root: incoming };
      return;
    }
    const root = content.value.root;
    if (parentId === null) {
      content.value = { ...content.value, root: { ...incoming, children: root.children } };
    } else {
      content.value = { ...content.value, root: upsert(root, parentId, incoming) };
    }
  }

  function select(id: string | null) {
    selectedId.value = id;
  }

  function setDirection(next: Direction) {
    direction.value = next;
    // 换方向后整体重排，视图必须重新适配，否则可能停在空白处。
    viewAdjusted.value = false;
  }

  /**
   * 不经过网络直接装载一份导图，开发期的示例选择器用它预览布局。
   * 走 cancel() 先掐掉在途请求，避免旧流回来后把示例顶掉。
   */
  function preview(next: MindMapContent, dir: Direction) {
    cancel();
    direction.value = dir;
    content.value = next;
    mode.value = 'demo';
    selectedId.value = null;
    viewAdjusted.value = false;
    status.value = '这是本地示例数据，用来检查布局与截断，不是模型生成的。';
  }

  function cancel() {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    stageText.value = '';
    status.value = '已取消。';
  }

  async function generate(text: string): Promise<void> {
    const prompt = text.trim();
    if (!prompt) {
      status.value = '请输入主题或文本。';
      return;
    }
    if (busy.value) return;

    // 先把上一份结果存起来：新请求失败时恢复它，而不是让用户白等一场还丢了旧图。
    const previous = content.value;
    const previousMode = mode.value;

    busy.value = true;
    waitedMs.value = 0;
    stageText.value = '正在连接';
    content.value = null;
    mode.value = null;
    selectedId.value = null;
    viewAdjusted.value = false;

    const ctrl = new AbortController();
    controller = ctrl;
    // settled 表示这次请求已经有了明确结局（成功或明确失败）；
    // 只有「既没 done 也没 error」才说明连接是被掐断的。
    let settled = false;

    const restore = () => {
      content.value = previous;
      mode.value = previousMode;
    };

    try {
      await generateMindMapStream(
        prompt,
        event => {
          switch (event.type) {
            case 'stage':
              stageText.value = event.detail;
              break;
            case 'skeleton':
              // 只有 demo 模式会发这个事件：它一次性给出整棵树，不走增量推送。
              content.value = event.content;
              break;
            case 'node':
              // 增量方案：节点一个个长出来，同一 id 的文字会越补越全。
              applyNode(event.parentId, event.node as MindNode);
              stageText.value = '节点生成中';
              status.value = '正在生成，节点会陆续出现…';
              break;
            case 'heartbeat':
              waitedMs.value = Math.round(event.waitedMs / 1000);
              break;
            case 'done':
              settled = true;
              content.value = event.content;
              mode.value = event.mode;
              status.value =
                event.mode === 'demo'
                  ? '这是固定结构的演示数据，不是 AI 生成。'
                  : `模型已生成候选导图，用时 ${(event.latencyMs / 1000).toFixed(1)} 秒。`;
              stageText.value = '';
              break;
            case 'error':
              settled = true;
              // 后端在流里报错时，之前那份导图同样要还回来，不能只留一个空画布。
              restore();
              status.value = event.message;
              stageText.value = '';
              break;
          }
        },
        ctrl.signal,
      );

      // 已经被后来的请求接管：这个旧请求无论成功、失败还是被取消，
      // 都不许再改画面和状态，否则新请求的预览会被旧导图替换。
      if (controller !== null && controller !== ctrl) return;
      if (ctrl.signal.aborted) {
        restore();
        status.value = '已取消。';
      } else if (!settled) {
        // 流正常结束但没有 done 帧：中途断了，不能假装成功。
        restore();
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== null && controller !== ctrl) return;
      restore();
      status.value = ctrl.signal.aborted
        ? '已取消。'
        : error instanceof ApiError
          ? error.message
          : '生成失败，请重试。';
      stageText.value = '';
    } finally {
      // 取消后立刻重新生成时，旧请求的收尾不能把新请求的状态清掉。
      if (controller === ctrl) {
        busy.value = false;
        controller = null;
      }
    }
  }

  return {
    content,
    mode,
    status,
    stageText,
    waitedMs,
    busy,
    selectedId,
    direction,
    viewAdjusted,
    layout,
    flatNodes,
    nodes,
    edges,
    nodeCount,
    selectedNode,
    breadcrumb,
    generate,
    cancel,
    preview,
    select,
    setDirection,
  };
});
