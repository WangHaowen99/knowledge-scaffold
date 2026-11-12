"""测试里调外部命令（git、bash、脚本）统一经这里：解析成绝对路径、参数列表、不经 shell。

CodeCheck 的命令执行安全热点只在这一处标注，各测试不再各自调 subprocess。
"""

import shutil
import subprocess


def resolve(program: str) -> str:
    """命令名解析成绝对路径；已是路径的原样返回。"""
    return shutil.which(program) or program


def run(args, **kwargs) -> subprocess.CompletedProcess:
    """同 subprocess.run，第一个参数按 PATH 解析成绝对路径。"""
    command = [resolve(str(args[0])), *map(str, args[1:])]
    return subprocess.run(command, **kwargs)  # NOSONAR(S4721) 测试辅助：固定参数列表、不经 shell，参数来自测试本身


def popen(args, **kwargs) -> subprocess.Popen:
    """同 subprocess.Popen，第一个参数按 PATH 解析成绝对路径。"""
    command = [resolve(str(args[0])), *map(str, args[1:])]
    return subprocess.Popen(command, **kwargs)  # NOSONAR(S4721) 测试辅助：固定参数列表、不经 shell，参数来自测试本身


def git(cwd, *args, check=True, **kwargs) -> str:
    """在 cwd 里跑 git，返回标准输出。"""
    result = run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check, **kwargs)
    return result.stdout
