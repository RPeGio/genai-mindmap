import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

/**
 * 对话时间线：这一页发生过的每一句话。
 *
 * 它取代的是"每次生成都在右栏堆一张卡片"那套东西。生成完对话框会淡出、只剩画布，
 * 于是"我刚才说了什么、它回了什么、改了几处"必须有一个**不占画布**的去处——
 * 就是右下角那个悬浮球。
 *
 * 三条设计决定：
 *
 * 1. **按时间排，不按轮次折叠**。用户常常是"改一句 → 生成 → 再改一句"，折叠之后
 *    每一折只有一行，翻起来要连点三下才看得见自己上一次说的原话。时间线一眼扫过去。
 * 2. **未读角标只数"面板关着的时候新增的条数"**。这是"不打断，但也不隐瞒"的落地：
 *    生成完对话框消失，结果不该跟着一起消失。
 * 3. **消息只活在内存里**。它不是台账——`runs` 表已经记了每次运行的结果。这里存的
 *    是"这一页说的话"，刷新就没了，与页面上的画布是同一个生命周期。
 */

export type ChatRole = 'user' | 'ai' | 'system';

export interface ChatMessage {
  /** 时间线里的唯一 id（同一毫秒两条也不会打架）。 */
  id: string;
  /** 发生时刻（ms）。排序与"刚才/时间"显示都读它。 */
  at: number;
  role: ChatRole;
  text: string;
  /**
   * 这条消息说的是哪一次运行的结果。
   *
   * `kind` 决定它在界面上的样子：`error` 用玫红、`partial` 用琥珀，其余是普通气泡。
   */
  kind?: 'said' | 'done' | 'partial' | 'error' | 'system';
  /** 这次真正发出去的模型名（缺省 = 对面没说）。 */
  model?: string | null;
  /** 服务端回报的模型名（缺省 = 没回报）。与 `model` 分开，界面才看得出路由。 */
  servedModel?: string | null;
  /** 模型的思考过程全文（走推理模型时有）。 */
  reasoning?: string;
  /** 落地规模与改动条数（`done` 才有）。 */
  nodes?: number;
  changes?: number | null;
}

let counter = 0;

function nextId(): string {
  counter += 1;
  return `m${counter}`;
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<ChatMessage[]>([]);
  /** 面板开着时新增的消息直接算已读——用户正看着，不该再给他一个角标。 */
  const open = ref(false);
  const unread = ref(0);

  const count = computed(() => messages.value.length);
  const hasUnread = computed(() => unread.value > 0);

  function push(message: Omit<ChatMessage, 'id' | 'at'> & { at?: number }): ChatMessage {
    const entry: ChatMessage = {
      // **先展开、最后写 `id` / `at`**：`said(text, at?)` 显式传进来的 `at` 可能是
      // `undefined`，展开在默认值之后会把它覆盖回去，时间线里那条消息就没有时间了
      // （显示成 `NaN:NaN`）。顺序反了就必然出这个洞。
      ...message,
      id: nextId(),
      at: Number.isFinite(message.at) ? (message.at as number) : Date.now(),
    };
    messages.value = [...messages.value, entry];
    if (!open.value) unread.value += 1;
    return entry;
  }

  /** 用户说的那一句。发出去的**原文**（`goal`），不是输入框里此刻的内容。 */
  function said(text: string, at?: number): ChatMessage {
    return push({ role: 'user', text, kind: 'said', at });
  }

  /** 模型回的那一句：规模、改动条数、模型名、思考过程都挂在这里。 */
  function replied(input: {
    text: string;
    kind?: ChatMessage['kind'];
    model?: string | null;
    servedModel?: string | null;
    reasoning?: string;
    nodes?: number;
    changes?: number | null;
    at?: number;
  }): ChatMessage {
    return push({ role: 'ai', kind: 'done', ...input });
  }

  /** 界面自己说的话（打开了一张图、导出成功之类）。永远不影响未读角标以外的语义。 */
  function note(text: string, kind: ChatMessage['kind'] = 'system', at?: number): ChatMessage {
    return push({ role: 'system', text, kind, at });
  }

  function openPanel(): void {
    open.value = true;
    unread.value = 0;
  }

  function closePanel(): void {
    open.value = false;
  }

  function toggle(): void {
    if (open.value) closePanel();
    else openPanel();
  }

  /** 新的一轮：清空时间线（`+` 那个按钮的另一半）。 */
  function clear(): void {
    messages.value = [];
    unread.value = 0;
  }

  return {
    messages, open, unread, count, hasUnread,
    push, said, replied, note,
    openPanel, closePanel, toggle, clear,
  };
});
