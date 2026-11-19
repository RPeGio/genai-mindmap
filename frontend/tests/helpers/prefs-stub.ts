import type { Pref, PrefSuggestion } from '../../src/api/types';

/**
 * 偏好接口的 fetch 桩。
 *
 * `App` 一挂载就会拉 `GET /prefs` 与 `GET /prefs/suggestions`——重生成面板的两个
 * 下拉要在用户动手之前就带着偏好，所以这是**产品行为**，不是测试噪音。
 *
 * 但它确实会挤进界面用例里那些"第几次请求""一共几次请求"的断言：一次生成变成了
 * 四条 fetch，而其中两条与被测动作毫无关系。所以每个界面用例的 fetch 桩先接住这两
 * 个请求、并且**不把它们记进 calls**——`calls` 记的应该只有被测动作发出去的东西。
 */
export function isPrefsRequest(url: string): boolean {
  return url.includes('/prefs');
}

/** 按 URL 给出对应的空列表。`/prefs/suggestions` 与 `/prefs` 是两个接口。 */
export function prefsReply(
  url: string,
  prefs: Pref[] = [],
  suggestions: PrefSuggestion[] = [],
): Response {
  const payload = url.includes('/suggestions') ? suggestions : prefs;
  return { ok: true, status: 200, json: async () => payload } as unknown as Response;
}

/** 一条图型偏好，供"预填"那几条用例造初始状态。 */
export function diagramPref(value: string, extra: Partial<Pref> = {}): Pref {
  return {
    key: 'diagram_type.default',
    value,
    scope: 'global',
    source: 'user_explicit',
    source_run_id: null,
    created_at: '2026-11-01T00:00:00.000Z',
    updated_at: '2026-11-01T00:00:00.000Z',
    last_applied_at: null,
    ...extra,
  };
}

/** 一条拆解方式偏好。 */
export function thinkingPref(value: string, extra: Partial<Pref> = {}): Pref {
  return { ...diagramPref(value, extra), key: 'thinking_model.default' };
}
