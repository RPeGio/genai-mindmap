import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import type { DiagramType, DiagramTypeChoice } from '../api/types';
import { DIAGRAM_PREF_KEY, THINKING_PREF_KEY } from './prefs';

/**
 * 「这次新建要生成成什么样」的两个选项：拆解方式与图型。
 *
 * 它们从 `SourceInputs` 里搬到了这个 store，理由只有一条：**下拉与消费它的按钮
 * 不再在同一个组件里**。材料区只剩下材料，按钮在画布右上角（`App.vue` 的 #2），
 * 两个组件都要读同一份值，所以状态必须有单一归属，不能各存一份再靠 props 同步。
 *
 * 选项写死在前端而不是拉接口：这一批不做那个接口，而写死的值正是后端 Literal
 * 允许的全集——两边不一致时后端会 422，不会静默变形。
 */

/** 值与后端 `generation.service.THINKING_MODELS` 的键一一对应。 */
export const THINKING_MODEL_OPTIONS: { value: string; label: string }[] = [
  { value: 'default', label: '通用' },
  { value: 'first-principles', label: '第一性原理' },
  { value: 'feynman', label: '费曼' },
  { value: 'systematic', label: '5W1H' },
  { value: 'critical', label: '批判性' },
];

/**
 * 图型。`auto` = 不传字段（后端 `None`，让模型自己判断）——它是默认值，也是
 * "用户没说"这件事在界面上唯一诚实的呈现：不选，就由生成那一次决定。
 *
 * 四个值与后端 `AgentRunRequest.diagram_type` 的 Literal 一一对应（`auto` 那一项
 * 不发出去）。
 *
 * `hint` 是**必须**的：图型不是自解释的选项。用户选「流程图」时，他期待的是
 * "一级分支按阶段排、判断节点分是/否"，而不是"换个颜色的思维导图"；选「鱼骨图」
 * 时更得先知道一级分支固定是那六个类别——否则他会以为模型没听懂他的主题。
 */
export const DIAGRAM_TYPE_OPTIONS: { value: DiagramTypeChoice; label: string; hint: string }[] = [
  { value: 'auto', label: '自动判断', hint: '按你的描述判断图型；判断不出时按思维导图生成。' },
  { value: 'tree', label: '思维导图', hint: '一级分支按话题分解，不画附加连线。' },
  { value: 'flowchart', label: '流程图', hint: '一级分支会按执行阶段排，判断节点带是/否分支' },
  { value: 'fishbone', label: '鱼骨图', hint: '一级分支固定为人/机/料/法/环/测' },
];

const DIAGRAM_VALUES: DiagramType[] = ['tree', 'flowchart', 'fishbone'];

function asChoice(value: string | undefined): DiagramType | null {
  return DIAGRAM_VALUES.find(item => item === value) ?? null;
}

export const useGenerationOptions = defineStore('generationOptions', () => {
  /** 拆解方式：`default` 与后端默认值一致。 */
  const thinkingModel = ref('default');
  const diagramType = ref<DiagramTypeChoice>('auto');

  /**
   * 用户**手动改过**没有。改过之后偏好不再覆盖它。
   *
   * 这条是"预填"与"偷偷填"的分界：偏好只决定**初始值**，一旦用户动过下拉，值就
   * 是他的，刷新偏好也不该把它顶回去。没有这一位的话，用户在面板里改一条偏好会
   * 顺手改掉他刚刚手动选的图型——而那个选择才是他此刻的意图。
   */
  const touched = ref({ thinking: false, diagram: false });

  /**
   * 当前值是不是**从偏好预填来的**。
   *
   * 界面据此标「流程图（来自你的偏好）」，请求层据此记 `prefs_applied`。
   * 它与 `touched` 不是互补的两种状态——一个值可以既没被用户动过、也不是偏好
   * 预填的（那就是 `auto` / `default`，谁都没说）。
   */
  const prefilled = ref({ thinking: false, diagram: false });

  const diagramHint = computed(
    () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.hint ?? '',
  );

  /** 图型下拉里那一项的显示名（`auto` 是「自动判断」）。 */
  const diagramLabel = computed(
    () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.label ?? '',
  );

  const thinkingLabel = computed(
    () => THINKING_MODEL_OPTIONS.find(item => item.value === thinkingModel.value)?.label
      ?? thinkingModel.value,
  );

  /**
   * 把偏好填成**初始值**。用户在界面上看得见、也改得掉——这是这一层的全部要害。
   *
   * 与"后端开流前把偏好塞进请求体"是两件事：那条路会让 `runs.request.diagram_type`
   * 记下"用户选了流程图"，而用户根本没选过。预填之后请求体里带的是用户能在下拉里
   * 看见的值，那一列的语义仍然是"这次请求实际是什么"。
   */
  function applyDefaults(defaults: Record<string, string>): void {
    if (!touched.value.diagram) {
      const choice = asChoice(defaults[DIAGRAM_PREF_KEY]);
      diagramType.value = choice ?? 'auto';
      prefilled.value.diagram = choice !== null;
    }
    const thinking = defaults[THINKING_PREF_KEY];
    if (!touched.value.thinking && thinking) {
      thinkingModel.value = thinking;
      prefilled.value.thinking = true;
    }
  }

  function chooseDiagram(value: DiagramTypeChoice): void {
    diagramType.value = value;
    touched.value.diagram = true;
    prefilled.value.diagram = false;
  }

  function chooseThinking(value: string): void {
    thinkingModel.value = value;
    touched.value.thinking = true;
    prefilled.value.thinking = false;
  }

  /**
   * 忘掉"用户改过"。在「以后都这样」把当前值写成偏好之后调一次。
   *
   * 不调的话，界面上会同时存在两个真相：偏好里写着流程图、下拉却停在用户上一次
   * 手动选的思维导图，而标着"来自你的偏好"的那行说明已经消失了。
   */
  function forget(): void {
    touched.value = { thinking: false, diagram: false };
    prefilled.value = { thinking: false, diagram: false };
  }

  /**
   * 这一次请求**实际带着哪些偏好**，形如 `["diagram_type.default=fishbone"]`。
   *
   * 只算"值来自偏好、且用户没动过"的那些。用户手动选过之后再选回同一个值时这里
   * 是空的——那是他自己选的，哪怕值碰巧一样。宁可少记一条，也不要把用户的选择
   * 说成系统的预填。
   */
  function appliedKeys(): string[] {
    const keys: string[] = [];
    if (prefilled.value.diagram && diagramType.value !== 'auto') {
      keys.push(`${DIAGRAM_PREF_KEY}=${diagramType.value}`);
    }
    if (prefilled.value.thinking && thinkingModel.value) {
      keys.push(`${THINKING_PREF_KEY}=${thinkingModel.value}`);
    }
    return keys;
  }

  return {
    thinkingModel,
    diagramType,
    diagramHint,
    diagramLabel,
    thinkingLabel,
    touched,
    prefilled,
    applyDefaults,
    chooseDiagram,
    chooseThinking,
    forget,
    appliedKeys,
  };
});
