<!-- BUSINESS_KNOWLEDGE_SNIPPET.md — transcludable fragment.
     Command files reference this verbatim into dispatched agent prompts;
     it is not itself an agent and must never be dispatched. -->

## 业务知识守则

业务知识的存放与加载属于基础建模能力：`knowledge_helper.py load` 取出的条目（项目知识，以及已启用的行业包知识）在每个命令里照常使用。

平台不提供新增知识条目的能力：只应用已沉淀的知识，**不要**调用 `knowledge_helper.py add` 新增条目；用户明确要求记下某条规则时，按建模口径写进对应的对象、规则或指标文档。

### 已沉淀知识的应用方式

agent prompt 顶部若出现 `# 已沉淀业务知识（自动加载）` 区块，请：
- 在所有判断/抽取步骤前先扫描该区块；
- 与当前任务相关的条目作为**硬约束**遵循（高置信度）或**软提醒**复核（中/低置信度）；
- 若发现已沉淀知识与当前材料矛盾，触发"业务知识确认类"对话让用户裁决。

### 用户面前的措辞

仅用：**业务知识 / 专家经验 / 沉淀**。
禁止：memory / knowledge base / frontmatter / 数据库 / 索引文件 等技术术语。
