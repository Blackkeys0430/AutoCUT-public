# 全量预设标准化产物与恢复说明

本产物只建立项目自有的机器清单与包记录，不复制、覆盖或删除任何原始预设，也不注册或写入剪映草稿。

## 结果

- 标准化记录：917 条；ready：24 条；blocked：893 条。
- 新分配稳定 ID：136 条，格式为 `JY-STD-<规范结构哈希前12位>`；已有 `JIANYING-*`/`MODERN-*` ID 原样保留。
- 三个直接来源：统一注册表 781 条、自动候选 779 条、筛选队列 286 条；各自无重复 ID。
- 结构总账额外纳入未索引结构，确保全库 915 个去重结构都有记录；未完成资源/槽位映射的记录保持 blocked。

## ready 定义

ready 仅表示源包存在、预览存在、版本可用于剪映 8.8、画布为 1080×1920（9:16）、没有图片/视频槽、文字轨契约完整且资源状态可用。它不等于任何具体口播视频的当前画面验收。

## 恢复/重建

1. 保留本文件、`standardized_preset_catalog_v1.json` 与 `standardized_preset_audit_v1.json`。
2. 源文件未被改写；如需恢复清单，重新运行：
   `python jianying-adapter/scripts/standardize_preset_catalog.py`
3. 由于输出不含运行时 timestamp，输入未变时重跑结果中的记录、ID、分类和 blocked 原因保持稳定。
4. 若源文件发生变化，先比较输出中的 `source_snapshot` SHA256，再决定是否接受新清单；旧 JSON 仍可作为恢复副本。

输入快照：`{"auto_candidates_sha256": "154220CE679A65A3DECD06C21B7CDDBEE46A312DF32FC4AB0904899AF41A3E4B", "library_index_sha256": "0156C4CB76EE81A6241416D1924FA0D269D00C0CB7CEBF7D034EA51EB59A2E65", "screening_queue_sha256": "BDBC81055C9A8E7DBB3409861A38A83EF0F83445E6412027EA5B73C4A8D6916F", "structure_ledger_sha256": "2614FA5D60C1FA56EF819E1B228B74C4F6085F574BBF7A0BD43EDEA06CB21C41", "usage_registry_sha256": "59B311CFA286DF1F13775A3C74BFC46E586EB5AC41BEF85BBD16A2AFD3398E83"}`
