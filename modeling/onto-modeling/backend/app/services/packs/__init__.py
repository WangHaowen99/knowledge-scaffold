"""插件运行时（详设 4.6、8.1；方案 D1～D6）。

- manifest：包清单（详设 5.2）、版本与兼容范围、热更新分级（详设 3.5）；
- repository：包仓库，每个版本一个不可修改的目录，附校验和；静态检查在 checks；
- lockfile：项目锁文件 `.onto/packs.lock`（进工作区 git）；
- assembler：把内核守卫和已启用的包组装成按内容寻址的装配目录，原子切换 `.claude`；
- runtime：项目侧的启用 / 升级 / 回滚 / 停用、生效时机与启动核对；
- contracts：后端按锁定版本加载的共享契约库（方案 A16、D5）；
- builtin：镜像自带的官方包（core）导入包仓库。

数据目录布局（方案第 3 章）：

    <数据目录>/packs/<名>/<版本>/      包仓库，不可修改
    <数据目录>/packs/<名>/<版本>.json  版本元数据（来源、发布时间、通道）
    <数据目录>/packs/channels.json     通道指针（stable / beta）与禁用版本
    <数据目录>/packs/uploads/<ID>/     上传与静态检查记录
    <数据目录>/assembled/<哈希>/       装配目录，锁文件相同的项目共用
"""
