# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的六张表。模型在 `src/models/workflow.py`。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析及签核。数据库端口只发布到本机。

工单「来源版本号」与喷涂状态等业务字段保存在 `observations.payload`（JSON）内，同单多版本以多行存放、按 `revision` 取最新，因此版本化不引入新表或表结构变更，无需额外迁移。
