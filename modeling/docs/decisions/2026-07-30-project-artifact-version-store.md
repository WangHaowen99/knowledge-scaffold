# 项目级知识包修订与一致性存储

## Context

当前正式知识包版本主要保存在 SQLite 的 `package_versions` 中，文件内容需要根据结构化规格重新渲染。该方式无法冻结每个版本当时的完整文件，也缺少文件级差异、历史恢复和乐观并发控制。

知识包文件与 SQLite 版本索引属于两个不同的存储系统。若进程在文件提交和索引写入之间退出，可能留下只有 Git 文件、没有 SQLite 索引的半提交。

Web 服务部署环境不保证安装 Git CLI，版本能力也不应依赖宿主机命令。

## Decision

- 每个项目在 `DATA_DIR/artifacts/<project-id>` 下使用独立的本地 Git 仓库。
- 应用通过 `isomorphic-git` 管理仓库，不调用宿主机 Git CLI。
- 结构化规格保存到 `model/package-spec.json`，覆盖结果保存到 `model/coverage.json`，最终知识包文件保存到 `package/`。
- 所有写入携带调用方读取时的 expected revision，并通过 CAS 拒绝过期写入。
- SQLite 的 `artifact_revisions` 只保存历史查询索引，`package_versions.revision_oid` 关联正式版本与文件快照。
- 历史恢复通过创建新修订实现，不删除或改写已有历史。
- Git 提交前写入 `artifact_commit_intents`；Git 成功后在单个 SQLite 事务中写入业务索引并删除提交意图。
- SQLite 索引失败时，仅在 HEAD 仍指向本次新修订时回退到直接父修订。服务重启后首次访问会回收未建立索引的尾部提交。
- 本阶段不切换现有业务路由，不迁移 `package_overrides`，也不增加前端版本工作区。

## Alternatives considered

- 继续只保存结构化规格并按需重新渲染：实现简单，但不能保证历史文件内容稳定，也无法提供可靠的文件差异与恢复。
- 直接调用系统 Git CLI：开发成本较低，但增加部署依赖，并扩大命令注入和环境差异风险。
- 把完整文件内容重复保存到 SQLite：可以单库事务化，但会形成两套文件真相并增加大文本查询、迁移和备份成本。
- 引入分布式事务：对当前本地 Git 与 SQLite 组合过重，仍无法直接让文件系统参加数据库事务。

## Consequences

- `DATA_DIR` 中的 SQLite 数据库和 `artifacts` 目录共同构成完整版本状态，必须在同一备份点恢复。
- 首次访问旧项目时，会按时间顺序为未关联的正式版本创建 Git 修订。
- 调用方可以稳定获得历史、差异、恢复和 revision conflict 语义。
- 索引写入失败后，本次 Git 提交不会对外可见，调用方可以沿用原 revision 重试。
- 多实例共享存储仍需保证 SQLite 文件锁和 artifacts 目录的一致读写；本决策不提供远程 Git 推送或拉取能力。
- 人工草稿及旧 `package_overrides` 的迁移保持独立，避免与现有修复工作重复。
