import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import type { DiagramTypeChoice } from '../api/types';

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

export const useGenerationOptions = defineStore('generationOptions', () => {
  /** 拆解方式：`default` 与后端默认值一致。 */
  const thinkingModel = ref('default');
  const diagramType = ref<DiagramTypeChoice>('auto');

  const diagramHint = computed(
    () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.hint ?? '',
  );

  return { thinkingModel, diagramType, diagramHint };
});
