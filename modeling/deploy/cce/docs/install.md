# Modeling CCE 安装

Modeling 软件包包含三个自有镜像以及用于 Core、Agent 和 Onto Modeling 的 Helm 发布。Core Pod 同时运行 JiuwenBox sidecar。JiuwenBox 镜像由 Consumer 软件包提供；共享的 auth-server 和 Consumer API 通过 Service 引用。本软件包不会安装第二个 auth-server。

环境要求：Python 3.11 及以上（含 PyYAML 和 jsonschema）、Helm 3、kubectl、具有部署权限的 Kubernetes context、预先创建的命名空间、三个独立的 RWO Filesystem PVC、镜像拉取 Secret、服务令牌 Secret，以及兼容 CCE 的 ELB Ingress 控制器。默认入口使用 HTTP；显式配置 HTTPS 时还需准备 TLS Secret。随附示例文件中的值均为合成值，必须在仓库外填写真实部署值。

1. 使用 `import-images.md` 导入三个自有镜像，同时导入 Consumer 软件包中的 JiuwenBox 镜像。将客户镜像仓库中的 manifest digest 记录到 `resolved-images.yaml`。示例中的 JiuwenBox digest 只是不可用的占位值；联合操作时应删除该项或替换为精确的 Consumer digest，联合入口会创建临时的有效镜像映射。单独执行 Modeling 操作时，必须显式提供经过验证的 Consumer digest。
2. 在 `values.customer.yaml` 中填写预创建 PVC 的名称、共享认证与 Consumer Service、issuer、audience 以及本次发布的 Ingress 路由。示例默认使用 HTTP 和监听端口 80；若两侧复用同一个 ELB，须分别分配监听端口。需要 HTTPS 时显式设置 `global.access.protocol: HTTPS`、监听端口及 `tlsSecretName`，并把 `modeling.config.mcpPublicBaseUrl` 改为实际对外地址。
3. 联合部署时提供双方共同验证的兼容性 manifest。包内 `compatibility.yaml` 是字段完整的模板，必须替换为实际已验证的组合；未验证的组合会被拒绝。单独 Modeling 安装不读取该参数。
4. 执行以下 `plan` 命令并检查 `STATE_DIR` 中渲染出的 YAML：

   ```bash
   bash deploy/install-cce.sh plan --context CONTEXT --namespace NAMESPACE \
     --release modeling --values values.customer.yaml \
     --resolved-images resolved-images.yaml \
     --state-dir STATE_DIR
   ```

5. 使用相同参数依次执行 `preflight`、`install` 和 `verify`。`--operation-id` 用于标识一次安装尝试；如果默认 900 秒不够，可设置 `--timeout-seconds`。

联合安装时，将两个软件包放在客户管理的 `deployment.customer.yaml` 旁边，并执行：

```text
bash deploy/install-consumer-modeling.sh cce install --config deployment.customer.yaml
```

联合入口遵循 Consumer 软件包的 `deploy/install-cce.sh` 契约，不读取 Consumer 源码。两侧分别拥有自己的 Helm 发布和运行时资源。入口会在调用任一侧前校验软件包校验和以及精确的兼容性组合。操作租约要求有权在两侧 release 所在命名空间创建、更新和释放各自的 `<releaseName>-cce-operation` Lease。客户 values、密钥和运行状态必须保留在发布包及 Git 之外。

当前预检会检查声明式资源和集群基础可用性，但不能证明所有候选节点都能拉取每个镜像，也不能证明选定 CCE 类型的 ELB 与 Ingress 注解可用，或特权 JiuwenBox 会被允许。这些内容必须在真实集群验收中验证。Pod 成功滚动也不能证明脚本执行正常；请在目标架构上执行真实的合成脚本验收。

数据保留升级请参阅 `upgrade-and-recovery.md`。升级要求经过测试的无迁移兼容组合、已停止的旧 Modeling Pod、就绪的 PVC 快照和经过验证的备份记录。联合执行顺序始终为 Consumer 升级并验证，然后 Modeling 升级并验证。Consumer 软件包的升级行为由其独立仓库负责。

源码检验时，Modeling 侧命令要求使用软件包目录结构。直接运行 Python 入口时，将 `--package-root` 指向解压后的 Modeling CCE 软件包；在交付包中，包装脚本会自动解析。

在没有开发镜像仓库、需要制作客户离线交付包时，请从仓库根目录、带 Docker Buildx 的构建机执行：

```bash
bash modeling/deploy/cce/build-offline.sh v1.0.0 amd64
```

默认输出为 `modeling/dist/modeling-cce-v1.0.0-amd64.tar.gz`，也可以传入第三个参数指定其他输出路径。`build-images.sh` 会准备并复用供应商输入，然后将三个镜像加载到本地 Docker；`build-offline.sh` 再将其打包。

Drawio 准备策略与虚拟机发布一致：如果已有 `modeling/onto-modeling/vendor/drawio/webapp/index.html` 就直接复用，否则尝试使用宿主机 `npm pack`。如果没有 npm 或准备失败，脚本会告警并继续构建，但交付包不包含 Drawio 编辑功能。若离线构建机需要该编辑器，请在构建前从联网构建机传入完整的 `vendor/drawio/webapp/` 目录。Docker 基础镜像和 Python 依赖也必须已在本地，或可从批准的镜像源获取。其他构建错误会终止脚本；重新执行会复用相同版本标签。

目标架构为 arm64 时必须始终使用 `arm64`。跨架构构建需要可用的仿真或原生节点。本地镜像标签为 `modeling-offline/modeling:<版本>`、`modeling-offline/modeling-agent:<版本>` 和 `modeling-offline/onto-modeling:<版本>`，标签不附加架构后缀；同一版本号分别构建两种架构时，后一次构建会覆盖本地同名标签，组包应紧跟对应架构构建。`package-cce.py --local` 会校验镜像架构并在不拉取或推送镜像的情况下归档。发布 manifest 记录镜像配置 digest 和归档 SHA-256；镜像未推送仓库前，其 `sourceManifestDigest` 为 null。导入客户 SWR 后，应使用目标 digest 进行部署。JiuwenBox 不由此任务构建或归档；原有虚拟机构建脚本和 tar 包流程仍独立存在。

构建机未安装 Helm 时，组包脚本会尝试临时下载官方 Helm。若下载失败，脚本直接复制 Chart 源目录进离线包；客户侧仍须安装 Helm 3，在 `plan` 和 `install` 阶段渲染 Chart。

开发镜像仓库流程请将 `REGISTRY_PREFIX` 作为 `build-images.sh` 的第三个参数；它会推送三个镜像。之后使用重复的 `--image id=repository@sha256:digest` 参数打包经批准的 digest 引用。
