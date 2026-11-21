/**
 * `POST /optimization/check` / `suggest` 的替身。
 *
 * 为什么每个挂载 `App` 的用例都要认得它：**生成成功之后前端会自动跑一次只读结构
 * 检查**（`ChatDock` 里那条 watch），所以每一份"生成落地"的用例都会多出一条背景
 * 请求。它与偏好、版本号是同一类东西——自己跑、不属于"点这个入口发出了什么"。
 *
 * 让它们进 `calls` 的后果是具体的：`calls` 的长度会多一、`calls[0]` 或"最后一条"
 * 会指向一个与断言无关的 POST，而用例原本钉的是"只发了一次生成请求" / "材料进了
 * 请求体"。那不是被测行为变了，是背景噪音换了位置。
 *
 * 替身回一个**空结论**（零诊断、零建议）：界面因此安静，不会给任何既有断言加上
 * 新的数字或新的区块。
 */
export function isOptimizationRequest(url: string): boolean {
  return url.includes('/optimization/');
}

export function optimizationReply(): Response {
  return new Response(JSON.stringify({ diagnostics: [], suggestions: [] }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}
