---
name: onto-plugin-dev-{{category}}
description: 开发 onto-modeling 平台的{{label}}。{{trigger}}时使用；开发者说「做一个插件」「写一个{{label}}」「把这件事做成插件」，或者要改一个已有的{{label}}、修静态检查报告里的问题时也用它。产出过了静态检查、能直接上传的 zip。
---

# 开发{{label}}

助手只读这个 skill 目录就能做出能上传的 zip，不需要平台源码。需求够写就直接做完，不追问；只有{{ask}}时才问开发者，一次问完。

下面的 `<skill>` 指这个 SKILL.md 所在的目录。`platform.json` 是下载时的平台快照，平台升级后重新下载 skill。

## 步骤

1. 理解需求：弄清这个包要让平台多出什么，用户在哪里用到它。
2. 选 kind：{{kind_choice}}
3. 建骨架：在开发者的工作目录运行 `python3 <skill>/scripts/pack_tool.py init <包名>{{init_kind}}`。包名是 2～40 位小写字母、数字、连字符，字母开头，不能和 `platform.json` 的 `packs` 重名。骨架由 `references/examples/` 的合成样例改名而来，样例内容要全部换掉，包括 `label`、`description`。
4. 写内容：`init` 只替换包名，骨架里的 `label`、说明、示例目录名和正文都要改成这个包自己的，用不到的文件、节点、命令删掉，清单的 `contributes` 同步改。按 `references/{{category}}.md` 写。字段、取值范围、限制查 `references/protocol.md`。
5. 打包：运行 `python3 <skill>/scripts/pack_tool.py build <包目录>`，zip 在包目录旁边的 `dist/`。有「失败」项就按说明改，再跑一遍，直到通过。校验和由这条命令写，不手改 `checksums.sha256`。工具只依赖 Python 3.9+ 和 PyYAML。
6. 交付：告诉开发者 zip 的路径，并说明下面几件事。
   - 到插件中心点「安装插件」上传 zip，静态检查通过后安装，再启用到项目。
   - `build` 输出里「上传时检查」那几项离线查不了，以上传后的检查报告为准。
{{closing}}

## 写包时的规矩

- 改已经装过的包要升 `version`，同名同版本不能再上传。
- 包里不放真实客户名、内部地址和凭据。示例数据用合成的，域名只用 example.com。
