import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { deletePref, listPrefs, listPrefSuggestions, setPref } from '../api/client';
import type { Pref, PrefSuggestion } from '../api/types';

/**
 * 用户偏好（跨请求、关于这个人的稳定事实）。
 *
 * 三层记忆里唯一缺的那一层：会话那层是 `runs` 里那棵树，工作区那层就是
 * `MindMapContent` 本身。这两层早就实现了，只是不叫这个名字——所以这里不造
 * 一个"统一记忆"的抽象，只补真正没有的那一层。
 *
 * **三条硬规矩，写在这里是为了别在某次改动里丢掉**：
 *
 * 1. 模型没有写记忆的权限。所有写入都来自用户动作（面板里改、或者点了建议）。
 *    自动总结会在"同一主题两分钟内两次相反要求"那里写出两条互相矛盾的事实。
 * 2. 建议只产出候选，必须经用户确认才 `PUT`。拒绝就是什么都不发生。
 * 3. 面板必须能改、能删、能看到"上次生效"。写进去却从不被读的一条记忆，只是
 *    "心里记着"——用户看不出它有没有用。
 */

/** 键名与后端 `prefs.service` 一一对应。写错一个字母的后果是"设了但没生效"。 */
export const DIAGRAM_PREF_KEY = 'diagram_type.default';
export const THINKING_PREF_KEY = 'thinking_model.default';
export const ICON_PREF_KEY = 'icon.enabled';
/**
 * 没材料时要不要先联网搜一轮资料（后端 `tools.external_knowledge_enabled` 是唯一
 * 读点）。存成**偏好**而不是请求字段：它是"这个人愿不愿意为每次生成多等几秒换
 * 看得见的出处"，跨请求稳定；做成请求字段的话，用户每点一次生成都要重新回答一遍。
 *
 * 它只是半个开关：部署方在环境变量里关掉时（`MINDMAP_EXTERNAL_KNOWLEDGE=0`），
 * 这条偏好改不动任何东西——优先级钉在后端一个函数里。
 */
export const EXTERNAL_SEARCH_PREF_KEY = 'external_search.enabled';
/** C 类自由文本备注的前缀。只有这一类会进提示词。 */
export const NOTE_PREFIX = 'note.';

/**
 * 值域是 `true` / `false` 的偏好。面板给它们画勾选框，而不是让人手打这两个单词：
 * 一个开关用文本框编辑，用户只能靠"上次生效"去猜自己有没有打错。
 */
export const BOOLEAN_PREF_KEYS: readonly string[] = [ICON_PREF_KEY, EXTERNAL_SEARCH_PREF_KEY];

/** 备注的条数上限，与后端 `service.MAX_NOTES` 同值。这里只用来**先拦一步**并
 *  显示"还剩几条"；权威判定在后端，超了会返回 `pref_limit_exceeded`。 */
export const MAX_NOTES = 8;

/** 下一个可用的备注键：取最小的空位。删掉中间一条后再加，不会把编号顶到 9。 */
export function nextNoteKey(keys: Iterable<string>): string {
  const taken = new Set(
    [...keys].filter(key => key.startsWith(NOTE_PREFIX)).map(key => key.slice(NOTE_PREFIX.length)),
  );
  for (let index = 1; index <= MAX_NOTES; index += 1) {
    if (!taken.has(String(index))) return `${NOTE_PREFIX}${index}`;
  }
  return `${NOTE_PREFIX}${MAX_NOTES + 1}`;
}

/**
 * 一条偏好的值的显示名。
 *
 * `icon.enabled` 的 `true` / `false` 直接显示会让人以为这是个技术字段；图型与拆解
 * 方式的中文名由后端在建议里给（`label`），面板里这两类仍显示原值——原值是可以
 * 直接对着 API 核对的，翻译一层反而多一处会漂的映射。
 */
export function prefValueLabel(key: string, value: string): string {
  if (key === ICON_PREF_KEY) return value === 'true' ? '给节点配图标' : '不给节点配图标';
  if (key === EXTERNAL_SEARCH_PREF_KEY) {
    return value === 'true' ? '没材料时先联网搜资料' : '没材料时不联网，直接生成';
  }
  return value;
}

/** 来源标签。用户要能分辨"这条是我自己设的"与"这是我同意了系统的建议"。 */
export function prefSourceLabel(source: Pref['source']): string {
  return source === 'suggestion_accepted' ? '系统建议（你已同意）' : '你设置的';
}

/** `2026-11-13T08:12:33.412Z` → `2026-11-13`。面板上只需要日期。 */
export function prefDate(value?: string | null): string {
  return value ? value.slice(0, 10) : '';
}

export const usePrefsStore = defineStore('prefs', () => {
  const items = ref<Pref[]>([]);
  const candidates = ref<PrefSuggestion[]>([]);
  /**
   * 初值是 `true`，不是 `false`。
   *
   * `App` 挂载时立刻会拉一次，而 `onMounted` 在首帧渲染**之后**才同步执行。初值写成
   * `false` 的话，首帧会显示"还没有任何偏好"、下一帧又变成"正在读取"——用户看到的
   * 是提示闪了一下，而不是"还没读完"。状态的初值应当是"还没有结论"，不是"结论是
   * 空的"。
   */
  const loading = ref(true);
  const error = ref('');
  /** 正在写的那条键（用于禁用按钮）。空串 = 没有正在进行的写入。 */
  const saving = ref('');
  /**
   * 本次会话里被拒绝的建议，形如 `key=value`。
   *
   * **不落库**：一次拒绝不需要留下痕迹，而落一张"已忽略建议"表要维护失效规则
   * （值变了要不要重提？），收益是零。刷新页面之后如果证据还在，它会再问一次
   * ——那时的"最近 3 次"已经是新的 3 次了。
   */
  const ignored = ref<string[]>([]);

  const byKey = computed<Record<string, Pref>>(() => {
    const table: Record<string, Pref> = {};
    for (const item of items.value) table[item.key] = item;
    return table;
  });

  const notes = computed(() =>
    items.value
      .filter(item => item.key.startsWith(NOTE_PREFIX))
      .sort((left, right) => left.key.localeCompare(right.key, 'en', { numeric: true })),
  );

  /** 还没被拒绝、也还没被采纳的建议。 */
  const suggestions = computed(() =>
    candidates.value.filter(item => !ignored.value.includes(`${item.key}=${item.value}`)),
  );

  /** A / B 类的键值表，供生成面板预填。 */
  const defaults = computed<Record<string, string>>(() => {
    const table: Record<string, string> = {};
    for (const item of items.value) {
      if (item.key.startsWith(NOTE_PREFIX)) continue;
      table[item.key] = item.value;
    }
    return table;
  });

  function value(key: string): string | null {
    return byKey.value[key]?.value ?? null;
  }

  /**
   * 联网检索开着没有。**没有这条偏好 = 开**，与后端的默认值一致。
   *
   * 判 `!== 'false'` 而不是 `=== 'true'`：默认开这件事只在一个地方定义（后端），
   * 这里判"不是明确关"，两种状态的映射只有一处会错。
   */
  const externalSearchEnabled = computed(() => value(EXTERNAL_SEARCH_PREF_KEY) !== 'false');

  /** 拉全部偏好与建议。**用 `Promise.allSettled`**：建议挂了不该让面板整块空。 */
  async function load(): Promise<void> {
    loading.value = true;
    error.value = '';
    const [prefs, hints] = await Promise.allSettled([listPrefs(), listPrefSuggestions()]);
    loading.value = false;
    if (prefs.status === 'fulfilled') {
      // 形状校验而不是直接赋值：旧后端没有这个接口，反代配错时也可能回一段 HTML
      // 或 `{}`。`items.value` 不是数组的后果不是"面板空着"，而是 `defaults` 那个
      // computed 里一句 `for...of` 把整个组件树的更新打断——界面上看起来像白屏。
      items.value = Array.isArray(prefs.value) ? prefs.value : [];
    } else {
      error.value = prefs.reason instanceof Error ? prefs.reason.message : '读取偏好失败。';
    }
    candidates.value = hints.status === 'fulfilled' && Array.isArray(hints.value) ? hints.value : [];
  }

  /**
   * 写一条。成功后就地更新，不重拉整个列表——重拉会把"刚写进去但服务端还没读到
   * 的运行记录"这类无关字段一起刷新，界面上看起来像别的条目也变了。
   */
  async function save(
    key: string,
    next: string,
    source: Pref['source'] = 'user_explicit',
  ): Promise<boolean> {
    saving.value = key;
    error.value = '';
    try {
      const saved = await setPref(key, next, source);
      const index = items.value.findIndex(item => item.key === key);
      if (index >= 0) items.value[index] = saved.pref;
      else items.value = [...items.value, saved.pref];
      // 采纳过的建议不再提；同一键的其它建议也一并作废（值已经变了）。
      candidates.value = candidates.value.filter(item => item.key !== key);
      return true;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '保存偏好失败。';
      return false;
    } finally {
      saving.value = '';
    }
  }

  async function remove(key: string): Promise<boolean> {
    saving.value = key;
    error.value = '';
    try {
      await deletePref(key);
      items.value = items.value.filter(item => item.key !== key);
      return true;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '删除偏好失败。';
      return false;
    } finally {
      saving.value = '';
    }
  }

  /** 用户拒绝了这条建议：只是本地不再显示，**不写任何东西**。 */
  function dismiss(item: PrefSuggestion): void {
    const token = `${item.key}=${item.value}`;
    if (!ignored.value.includes(token)) ignored.value = [...ignored.value, token];
  }

  /** 用户在建议上点了"设为默认"。 */
  function accept(item: PrefSuggestion): Promise<boolean> {
    return save(item.key, item.value, 'suggestion_accepted');
  }

  return {
    items,
    notes,
    suggestions,
    defaults,
    loading,
    saving,
    error,
    externalSearchEnabled,
    load,
    save,
    remove,
    dismiss,
    accept,
    value,
  };
});
