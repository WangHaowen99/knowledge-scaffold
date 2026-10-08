#!/bin/bash
# Onto Modeling 部署预检（方案第八节）：AVX 指令集、docker、内存/swap/磁盘。
# 独立脚本，可在解包后的交付目录或源码 deploy/ 目录直接运行。

set -euo pipefail

FAIL=0
warn() { printf '[警告] %s\n' "$1"; }
fail() { printf '[失败] %s\n' "$1"; FAIL=1; }
ok()   { printf '[通过] %s\n' "$1"; }

[ "$(uname -s)" = "Linux" ] || { fail "仅支持 Linux 部署"; exit 1; }

# 1. CPU 指令集：捆绑的 Claude Code CLI 是 Bun 编译的原生二进制，x86_64 必须暴露 AVX，
#    否则子进程直接 SIGILL 崩溃。常见原因不是物理 CPU 太老，而是虚拟化层用了
#    通用 CPU 模型（如 qemu64）把 AVX 屏蔽了。
MACHINE="$(uname -m)"
if [ "${MACHINE}" = "x86_64" ] || [ "${MACHINE}" = "amd64" ]; then
    AVX="$(grep -o 'avx[^ ]*' /proc/cpuinfo | head -1 || true)"
    if [ -n "${AVX}" ]; then
        ok "CPU 暴露 AVX 指令集（${AVX}）"
    else
        fail "CPU 未暴露 AVX 指令集，捆绑 CLI 将无法运行。整改阶梯（按优先级）：
  1) 虚拟化层整改：CPU 模型改为 host-passthrough 或任一带 AVX2 的具体型号（零代码成本）
  2) 换用 ARM 交付包（aarch64 无此限制）
  3) 降级兼容包：Node 22 + 旧版 JS 交付 CLI，容器内设 ONTO_WEB_CLI_PATH 指向（见 README，最后手段）
  4) 以上均不可行则不支持该环境"
    fi
else
    ok "非 x86_64 架构（${MACHINE}），无 AVX 限制"
fi

# 2. docker 与 compose
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ok "docker 可用"
else
    fail "docker 未安装或 daemon 不可达"
fi
if docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; then
    ok "docker compose 可用"
else
    fail "docker compose 不可用（docker compose / docker-compose 均未找到）"
fi

# 3. 内存：每活跃会话一个子进程（均值约 400MB），8G ≈ 12-15 并发会话、30-50 在线用户；
#    几十用户目标建议 16G（方案 4.1.1）
MEM_KB="$(grep MemTotal /proc/meminfo | grep -o '[0-9]*')"
MEM_GB=$((MEM_KB / 1024 / 1024))
if [ "${MEM_GB}" -ge 15 ]; then
    ok "内存 ${MEM_GB}G"
elif [ "${MEM_GB}" -ge 7 ]; then
    warn "内存 ${MEM_GB}G：可支撑约 12-15 并发活跃会话；几十用户同时建模建议 16G，并保持 ONTO_WEB_MAX_SESSIONS=12"
else
    fail "内存 ${MEM_GB}G 不足，最低按 8G 规划（方案 4.1.1）"
fi

# 4. swap/zram 兜底（方案 4.1.1：建议 2-4G，吸收峰值避免临界 OOM）
SWAP_KB="$(grep SwapTotal /proc/meminfo | grep -o '[0-9]*')"
if [ "${SWAP_KB}" -gt 0 ]; then
    ok "swap $((SWAP_KB / 1024 / 1024))G"
else
    warn "未配置 swap/zram，建议配置 2-4G 吸收内存峰值"
fi

# 5. 磁盘：workspace + 会话历史 + 镜像
DISK_AVAIL_GB="$(df -BG --output=avail . | tail -1 | grep -o '[0-9]*')"
if [ "${DISK_AVAIL_GB}" -ge 20 ]; then
    ok "当前目录所在磁盘可用 ${DISK_AVAIL_GB}G"
else
    warn "磁盘可用仅 ${DISK_AVAIL_GB}G，镜像加载与 workspace 增长可能吃紧，建议 ≥20G"
fi

echo
if [ "${FAIL}" -eq 1 ]; then
    echo "预检未通过，请先处理上述 [失败] 项。"
    exit 1
fi
echo "预检通过。"
