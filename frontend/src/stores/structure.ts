import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { checkOptimization, suggestOptimization } from '../api/client';
import type { DiagramContent, Source } from '../api/types';
import {
  normalizeDiagnostics,
  normalizeSuggestions,
  structureCounts,
  type DiagnosticNote,
  type SuggestionNote,
} from '../mindmap/diagnostics';
import { findIn, isTree } from '../mindmap/shape';
import { useMindMapStore } from './mindmap';

/**
 * 「应用」这条动作的分派表：`kind` → 具体做什么。
 *
 * **表外的一律没有按钮**（`APPLICABLE_KINDS` 是同一份清单的两面）。加一种能自动应用
 * 的建议类型时，改两处：这里加一项、`diagnostics.ts` 的白名单加一个 kind。
 *
 * 现在只有 `dedupe`：把那个重复的节点删掉。`merge` / `link` / `fill` / `drop` 后端
 * 还没给出"应用它到底改成什么"的定义（合并进哪个节点？补什么环节？），所以界面只
 * 显示。给它们摆按钮的结果是：点了要么没反应、要么替后端猜一个它没定义的操作。
 */
const APPLIERS: Record<string, (nodeId: string) => void> = {
  dedupe: nodeId => useMindMapStore().deleteNode(nodeId),
};

/**
 * 结构检查（`/optimization/check` 与 `/optimization/suggest`）的状态。
 *
 * ## 为什么它是独立的 store，而不是塞进 `useMindMapStore`
 *
 * 这两条函数描述的是"**画布内容的一份体检报告**"，与画布本身的生命周期不同：
 * 画布每次编辑都换一个 `content` 引用，而体检是**某一刻**的快照。分开之后那件事
 * 可以被如实说出来（`stale`），而塞进 mindmap store 的话，它迟早会被当成"实时的
 * 结论"来读——于是用户改了两笔，面板上还挂着"同级重名"，而那个重名已经被他改掉了。
 *
 * ## 两条路径，成本不同
 *
 *  - **自动**：生成落地之后跑一次 `check`，**不带 sources**。它只做结构三件套
 *    （单子节点 / 深度上限 / 同级重名），不落库、不调模型，请求体是那棵树。
 *  - **手动**：用户点「检查结构」时跑 `check` + `suggest`，**带上 sources**。带上之后
 *    后端会按原文核验每条引用（第三层），那条链是"点引用能跳回原文"的依据；
 *    代价是请求体里有材料全文，所以它不该在用户没点的时候发生。
 *
 * 自动路径刻意不跑 `suggest`：它现在还是纯规则（不花钱），但后端正在给它加模型层，
 * 那时"每次生成完自动跑"就变成了"每次生成完自动花一次模型调用"。界线画在"用户点了
 * 才发"这一侧，后端加成本时前端不用回来改。
 */
export const useStructureStore = defineStore('structure', () => {
  const map = useMindMapStore();

  const diagnostics = ref<DiagnosticNote[]>([]);
  const suggestions = ref<SuggestionNote[]>([]);

  /**
   * 用户明确说过"这条不用管"的键（`DiagnosticNote.key` / `SuggestionNote.key`）。
   *
   * **只在本会话内有效，不写库**：一次拒绝不需要留下任何记录（与偏好建议"点不用
   * 什么都不发生"同一条口径）。而它必须挡住"重新检查之后又冒出来"——后端的规则是
   * 纯函数，同一份内容重跑得到同一个 message，于是键也相同。
   */
  const dismissed = ref<Set<string>>(new Set());

  /** 正在检查。它只控制按钮的禁用态与一句"检查中"，不挡画布。 */
  const busy = ref(false);

  /** 上一次检查错在哪；空串表示没错。**不写进状态行**：它是抽屉里的一句提示。 */
  const error = ref('');

  /** 上一次检查跑完了没有。空结果与"没检查过"必须分开，见 `neverRun`。 */
  const ran = ref(false);

  /**
   * 这份报告是**哪一份内容**的体检结论（引用相等）。
   *
   * 它是 `stale` 的唯一判据：画布内容换过引用之后，报告说的就不是眼前这张图了。
   * 用引用而不是比较序列化：`applyContent` 每次整体替换 `content.value`，引用相等
   * 恰好等于"一个字都没变"（与 `generatedContent` / `editBase` 同一条口径）。
   */
  const checkedContent = ref<DiagramContent | null>(null);

  /** 这次报告里包不包含 `suggest`（手动路径才包含）。界面据此说清"应用"为什么不在。 */
  const withSuggestions = ref(false);

  /** 请求令牌：花名册外的响应（用户已经又点了一次 / 换了图）一律丢弃。 */
  let token = 0;

  /** 忽略过的条目从这里被滤掉，而不是从数组里删——重新检查之后它们还要被挡住。 */
  const visibleDiagnostics = computed(
    () => diagnostics.value.filter(note => !dismissed.value.has(note.key)),
  );
  /**
   * 一条建议指向的节点还在不在。
   *
   * 报告是**某一刻的快照**：用户可能已经手动删掉了那个节点。留一条指向"不存在的
   * 节点"的建议，点「应用」只会换来一句"已经不在图里了"——那它一开始就不该显示。
   *
   * 它顺手还挡掉了应用之后的一种复活：删掉节点之后再重跑，如果后端仍给出同一条
   * 建议（规则比想象中宽松、或者它指的是另一个同 id 的节点），这里会先把那条挡在
   * 外面——"点了应用它还在"是最让人不信任这个面板的形状。
   */
  function stillLive(note: SuggestionNote): boolean {
    if (!note.nodeId) return true;
    const content = map.content;
    if (!content) return true;
    return findIn(content, note.nodeId) !== null;
  }

  /**
   * `dedupe` 的「应用」= **删掉那个节点**，所以只对叶子放行。
   *
   * 这条限制是后端这一版加出 `NEAR_DUPLICATE_LABEL` 之后必须有的：那条诊断的文案是
   * "只多了「有」这 1 个字，请确认是否指的是同一件事"，它把这个判断**交给了人**，
   * 而它给出的建议 kind 同样是 `dedupe`。
   *
   * 于是"应用它"在两种情形下后果完全不同：
   *   · 完全重名的两个叶子 → 删掉多余的那个，零损失；
   *   · 近义的两个节点，其中一个下面还有 5 个节点 → 删掉就**丢了那一支**。而按钮上
   *     只写着「应用」，用户以为自己是在合并。
   *
   * 真正的合并（把子节点搬到保留的那个下面）需要人决定"并进哪一个、深度还对不对"，
   * 那是另一次编辑，不是一次点击。所以这里**只对叶子给按钮**，其余说清原因——
   * 少一个能点的按钮，好过一次静默丢内容。
   */
  function leafOnly(note: SuggestionNote): SuggestionNote {
    if (!note.applicable || !note.nodeId) return note;
    const content = map.content;
    if (!content) return note;
    const node = findIn(content, note.nodeId);
    if (!node || node.children.length === 0) return note;
    return {
      ...note,
      applicable: false,
      blockedReason: `这一支下面还有 ${node.children.length} 个节点：删掉会丢内容，`
        + '要合并请先把它下面的节点挪过去',
    };
  }

  /** 忽略过的、指向已不存在节点的、以及**有子节点不能一键删除**的，都在这里被标注/滤掉。 */
  const visibleSuggestions = computed(
    () => suggestions.value
      .filter(note => !dismissed.value.has(note.key))
      .filter(stillLive)
      .map(leafOnly),
  );

  /**
   * 状态行与按钮读的那三个数。
   *
   * 从**过滤之后**的列表算：用户点了忽略，状态行上的数字必须立刻跟着减——否则
   * "3 条建议"会一直挂着，点进去只剩两条，用户会以为界面在骗他。
   */
  const counts = computed(() => structureCounts(visibleDiagnostics.value, visibleSuggestions.value));

  /** 这件事有必要让用户看见吗（状态行/入口按钮据此存在）。 */
  const hasFindings = computed(() => counts.value.attention > 0 || counts.value.suggestions > 0);

  /**
   * 报告过期了：画布内容已经与体检那一刻不同。
   *
   * **不自动重跑**。自动重跑会在用户每改一个字时发一次请求，而编辑是连续动作；
   * 更重要的是，"过期"这件事本身是**该被说出来**的：用户改掉那个重名之后，面板上
   * 挂着"同级重名"而没有标记，他就会以为界面在说他没改过。
   */
  const stale = computed(
    () => checkedContent.value !== null && map.content !== checkedContent.value,
  );

  /** 从来没检查过。空列表 + `ran=false` 与"检查了，一条都没有"是两件事。 */
  const neverRun = computed(() => !ran.value);

  /**
   * 一次体检。
   *
   * `tree` 之外的内容直接返回 `'unsupported'`：后端 `optimization/service.py::check()`
   * 的签名只认 `MindMapContent`（它直接读 `content.root`），鱼骨与流程传过去会在服务端
   * 炸成 500。那不是前端能修的事（后端正在改），所以这里拦在发请求之前，并给出一句
   * 能读懂的话——比让用户看见一个 500 好。
   */
  async function run(options: { withSuggest?: boolean; sources?: Source[] } = {}): Promise<
    'ok' | 'unsupported' | 'empty' | 'failed'
  > {
    const content = map.content;
    if (!content) return 'empty';
    if (!isTree(content)) {
      diagnostics.value = [];
      suggestions.value = [];
      ran.value = true;
      checkedContent.value = content;
      withSuggestions.value = false;
      error.value = '结构检查现在只支持思维导图：后端的诊断只遍历树的根节点，鱼骨图与流程图还进不去。';
      return 'unsupported';
    }

    const mine = ++token;
    busy.value = true;
    error.value = '';
    const withSuggest = options.withSuggest === true;
    try {
      if (withSuggest) {
        const result = await suggestOptimization(content, options.sources);
        if (mine !== token) return 'ok';
        diagnostics.value = normalizeDiagnostics(result.diagnostics);
        suggestions.value = normalizeSuggestions(result.suggestions);
      } else {
        const result = await checkOptimization(content, options.sources);
        if (mine !== token) return 'ok';
        diagnostics.value = normalizeDiagnostics(result.diagnostics);
        // 自动路径不清建议：用户手动看过的那批候选还在，下一次生成落地时它们才作废
        // （见 `reset`）。这里清掉会让"刚看完的建议"被任意一次自动检查抹去。
      }
      if (mine !== token) return 'ok';
      withSuggestions.value = withSuggest || withSuggestions.value;
      ran.value = true;
      checkedContent.value = content;
      return 'ok';
    } catch (caught) {
      if (mine !== token) return 'ok';
      // 体检失败不改任何既有结论：它是一份附加信息，拿不到就当没拿到。
      error.value = caught instanceof Error ? caught.message : '结构检查没跑成，请重试。';
      return 'failed';
    } finally {
      if (mine === token) busy.value = false;
    }
  }

  /** 自动路径：生成落地之后跑一次免费的结构体检（不带 sources、不跑 suggest）。 */
  function checkAfterGenerate(): void {
    void run();
  }

  /** 手动路径：「检查结构」按钮。带上材料，因此第三层引用核验也会跑。 */
  function runFull(sources?: Source[]): void {
    void run({ withSuggest: true, sources });
  }

  /**
   * 一条建议的**应用动作**，按 `kind` 分派。
   *
   * 白名单在 `mindmap/diagnostics.ts::APPLICABLE_KINDS` 里——表外的一律到不了这里
   * （`note.applicable` 为假）。现在只有 `dedupe`：删掉那个重复的节点。
   *
   * 三条规矩：
   *  1. **绝不自动应用**。只有用户点「应用」才到这里——后端也没有自动应用接口。
   *  2. **走现有的编辑路径**（`map.deleteNode`）：它压撤销栈、清悬空边、把选中挪到
   *     父节点。绕过去自己改 `content` 就等于造了一条不能 Ctrl+Z 的修改。
   *  3. 应用之后**本地移除那条建议**，再顺手重跑一次体检：删掉之后后端会给出新的
   *     结论（可能还剩一条重名），而重跑失败也不影响"它已经不在了"这件事。
   */
  function applySuggestion(note: SuggestionNote): boolean {
    if (!note.applicable || !note.nodeId) return false;
    const content = map.content;
    if (!content) return false;
    const node = findIn(content, note.nodeId);
    // 节点已经不在了（用户先手动删了，或者这是一份过期报告）：真话说出来，而不是
    // 假装应用成功。
    if (!node) {
      map.status = '这条建议指向的节点已经不在图里了，重新检查一次。';
      return false;
    }
    if (map.isContainerRootId(note.nodeId)) {
      map.status = '这条建议指向的是中心主题 / 骨 / 阶段本身，不能删。';
      return false;
    }
    // 同一道闸门在动作这一层再判一次：按钮的显隐由 `visibleSuggestions` 决定，
    // 但那只是一层 UI；`applySuggestion` 是公开方法（测试、将来的快捷键都会直调），
    // 删掉一棵有内容的子树是不可撤销之外的损失（撤销也只救回"这一次会话"）。
    if (node.children.length > 0) {
      map.status = `「${node.label.trim()}」下面还有 ${node.children.length} 个节点，`
        + '要合并请先把它下面的内容挪过去。';
      return false;
    }

    const applier = APPLIERS[note.kind];
    if (!applier) return false;
    applier(note.nodeId);

    suggestions.value = suggestions.value.filter(item => item.key !== note.key);
    // 那条诊断（`DUPLICATE_LABEL`）说的是同一个节点，一起收掉：留着它就是在提醒
    // 一件已经做完的事。
    diagnostics.value = diagnostics.value.filter(item => item.nodeId !== note.nodeId);
    map.status = `已按建议删除重复节点「${node.label.trim()}」，可撤销。`;
    // 不带 sources 重跑：这是一次"看看还剩什么"的复核，没必要把材料全文再发一遍。
    void run({ withSuggest: true });
    return true;
  }

  /** 「忽略」：不写库，本会话内不再出现（见 `dismissed`）。 */
  function dismiss(key: string): void {
    const next = new Set(dismissed.value);
    next.add(key);
    dismissed.value = next;
  }

  /**
   * 报告作废（换了一张图、或者新一轮生成落地）。
   *
   * `dismissed` 也一起清：那些键属于上一张图 / 上一轮内容，留着只会在新图上偶然
   * 挡住一条恰好同键的诊断——而"我明明没忽略过这条"是最难查的一类问题。
   */
  function reset(): void {
    token += 1;
    diagnostics.value = [];
    suggestions.value = [];
    dismissed.value = new Set();
    error.value = '';
    ran.value = false;
    busy.value = false;
    withSuggestions.value = false;
    checkedContent.value = null;
  }

  return {
    diagnostics,
    suggestions,
    visibleDiagnostics,
    visibleSuggestions,
    dismissed,
    busy,
    error,
    ran,
    neverRun,
    stale,
    withSuggestions,
    counts,
    hasFindings,
    // 「有 warn 或 error 吗」：抽屉里 info 折叠区的标题用它决定怎么说
    attentionCount: computed(() => counts.value.attention),
    run,
    checkAfterGenerate,
    runFull,
    applySuggestion,
    dismiss,
    reset,
  };
});
