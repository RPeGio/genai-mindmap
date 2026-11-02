## 这段代码做什么

<!-- 一句话说明改动，对应 docs/FEATURES_AND_OWNERS.md 的 F 编号（若有）。 -->

## 为什么这么写

<!-- 选了哪种方案、放弃了什么、边界在哪。不许写"按要求实现"。 -->

## 我怎么验证的

<!-- 命令 / 截图 / 手工步骤。CI 绿不等于验证过。 -->

---

## Checklist

- [ ] 提交信息是中文 Conventional Commits，单提交 ≤ 半天工作量
- [ ] 本地跑过测试（后端 `pytest -q`，前端 `npm test`）
- [ ] 未提交任何密钥、token、`.env`、模型输出缓存
- [ ] 若改契约：`docs/CONTRACT_V2.md` 已同步，且 `migrate_v1_v2` 有迁移用例
- [ ] 若改契约：前端 `src/api/schema.d.ts` 已重新生成并一并提交
- [ ] 若加接口：`docs/API.md` 已更新（含错误码与 501/未实现标注）
- [ ] 未实现的功能返回 501 + `implemented: false`，**没有**返回空对象假装成功
- [ ] AI 产出仍然只是候选，不会覆盖用户手工编辑

> 合并前必须由**另一人** review 并 assign；契约相关改动需双人确认。
