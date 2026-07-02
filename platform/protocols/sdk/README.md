# SDK 目录

统一的 SDK 代码放在 `platform/protocols/sdk/` 目录下，按语言拆分为独立包：

- `typescript-sdk/`：TypeScript SDK
- `python-sdk/`：Python SDK

## 常用命令

### 构建全部 SDK

```bash
cd platform/protocols/sdk
./build.sh all
```

### 打包全部 SDK

```bash
cd platform/protocols/sdk
./package.sh all
```

### 单独构建 TypeScript SDK

```bash
cd platform/protocols/sdk/typescript-sdk
./build.sh
```

### 单独构建 Python SDK

```bash
cd platform/protocols/sdk/python-sdk
./build.sh
```

## 对外使用文档

如果需要把 SDK 提供给其他项目使用，请优先参考：

- `platform/protocols/sdk/PUBLISHING.md`
