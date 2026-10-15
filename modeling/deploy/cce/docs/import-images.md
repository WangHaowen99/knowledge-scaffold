# 离线镜像导入

`release.yaml` 列出三个 Modeling 镜像归档、各自的 SHA-256、镜像配置 digest 以及唯一目标架构。本机构建的镜像使用 `sourceManifestDigest: null`；来自镜像仓库的镜像记录经过验证的源 manifest digest。导入前请使用 `checksums.sha256` 校验每个文件。使用客户批准的镜像工具加载每个归档，并将其 `archiveRef` 推送到客户 SWR 命名空间。

上传后查询目标仓库的 manifest，并将生成的完整 `repository@sha256:digest` 引用记录到 `resolved-images.yaml`。不要假设镜像经过仓库转换后源 digest 仍然不变。

JiuwenBox 只是声明的依赖项。首次联合安装时，从 Consumer 软件包导入其镜像归档，让两套发布共同使用导入后的引用；两套发布会运行各自的实例。单侧升级期间，在精确兼容组合验证完成并明确更新 Modeling 发布前，应保留旧 digest；同时保留旧镜像以便恢复。软件包不包含 SWR 凭据、kubeconfig 或应用 Secret。

`release.yaml.chartDelivery` 记录 Chart 的组包方式：`helm-tgz` 表示构建机用 Helm 整理 Chart；`chart-source` 表示构建机无法取得 Helm，直接复制完整 Chart 源目录。两种包均在 `deploy/chart/` 提供可安装的 Chart。客户侧安装机需要 Helm 3，实际渲染发生在 `plan` 和 `install` 阶段。
