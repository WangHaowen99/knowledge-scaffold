#!/bin/sh
# code-repo 克隆时 git 的 askpass（ADR-0084）：随镜像发布、固定路径。网关的 /tmp 是 noexec 的 tmpfs，
# 临时写的脚本执行不了；令牌仍在任务目录的 0600 文件里，环境变量里只有用户名和这个文件的路径。
case "$1" in
  Username*) printf '%s\n' "$ONTO_GIT_ASKPASS_USER" ;;
  *) cat "$ONTO_GIT_ASKPASS_FILE" ;;
esac
