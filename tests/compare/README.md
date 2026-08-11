# tests/compare · M2 专用测试

> 目的（ADR-0005 决策 8 / ADR-0006）：M2 双验证（crepe_notes vs basic-pitch）的验收物。

同一哼唱样本喂 crepe_notes / basic-pitch → 统一 Voice schema → 评估指标
（自足指标 + 参考谱精确率/召回率）→ 产出对比报告。**M2 开始填充。**

当前骨架：
- `test_eval_interfaces.py`：eval 接口存在性 + NotImplementedError 守门（schema 冻结）
- M2 时在此目录放：`test_compare_hum.py`（哼唱子集）、`test_compare_vocal.py`（人声子集）
