#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create-ccache-mirror.py — 泛化镜像整个工具链树: 给定根目录与编译器 bin 的相对路径
(bin_rel), 沿路径逐层下行, 每层把除路径外的兄弟条目尽可能在外层整体符号链接
(整个子树一个链接); 最后一层 bin 内的编译器替换为调用缓存工具 (sccache/ccache)
的 ELF 包装脚本。不区分 llvm-mingw / ohos-sdk。

由 scripts/ccache.sh 调用: bash 逐条 fork cp/ln/basename/python3 太慢 (llvm-mingw
bin 约 700 个条目), 本脚本用 Python 单进程完成全部镜像。

用法:
    create-ccache-mirror.py <real_root> <shadow_root> <cache_tool> <bin_rel> [is_mingw] [fallback_cc]

    real_root   工具链根目录 (如 OHOS SDK 或 llvm-mingw 根)
    shadow_root 影子根目录 (镜像输出)
    cache_tool  缓存工具 (sccache/ccache) 路径
    bin_rel     编译器 bin 相对路径 (如 native/llvm/bin 或 bin)
    is_mingw    1 表示 bin 内按 llvm-mingw 三元组包装器规则处理 (可选)
    fallback_cc 编译包装器用的兜底编译器 (可选)

输出进度 (stdout, 每处理一个条目输出一个 '.'), 失败返回非零。
"""
import glob
import os
import re
import shutil
import subprocess
import sys

# 编译器名字模式: clang / clang++ / clang-cl / clang-cpp 及其版本化形式 (clang-15,
# clang-15.0.6, clang++-15, clang-cl-15 ...); 与 clang-format / clang-tidy 等工具区分。
COMPILER_RE = re.compile(r"^(clang(\+\+)?(-cl|-cpp)?)(-\d+[\d.]*)?$")
# OHOS SDK 的三元组包装器 (如 x86_64-unknown-linux-ohos-clang / -clang++)
OHOS_TRIPLE_RE = re.compile(r".*-unknown-linux-ohos-clang(\+\+)?$")
# llvm-mingw 的共享包装脚本 (三元组条目都是指向它的符号链接)
MINGW_WRAPPER_SH = "clang-target-wrapper.sh"


def _write(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def dot():
    _write(".")


def newline():
    _write("\n")


def rm_if_exists(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def c_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ELF 包装器 C 源码: exec <缓存工具> <真实编译器> "$@"。用 clang 从 stdin 编译成
# 原生 ELF, 使 file 识别为 ELF (可通过 build_ohos_guest_gfx.sh 的 is_native_elf 检查,
# 避免被再包一层 WSL 包装器)。无头文件版: 自行声明 execv/malloc, 任何能链接宿主
# 二进制的 clang 都能编译 (不需要 unistd.h/stdlib.h)。
WRAPPER_C = (
    "void *malloc(unsigned long n);\n"
    "int execv(const char *path, char *const argv[]);\n"
    "int main(int argc, char **argv) {\n"
    '    char *tool = "%s";\n'
    '    char *compiler = "%s";\n'
    '    char **nargv = (char **)malloc((unsigned long)(argc + 2) * sizeof(char *));\n'
    "    int i;\n"
    "    if (!nargv) return 127;\n"
    "    nargv[0] = tool;\n"
    "    nargv[1] = compiler;\n"
    "    for (i = 1; i < argc; i++) nargv[i + 1] = argv[i];\n"
    "    nargv[argc + 1] = 0;\n"
    "    execv(tool, nargv);\n"
    "    return 127;\n"
    "}\n"
)


def wrapper_cc():
    """选择编译包装器的编译器: 用系统 `cc` (宿主 C 编译器)。
    cc 在 OHOS SDK / llvm-mingw 中肯定不存在, 因此不会误选到无法链接宿主二进制的
    clang (macOS 上 OHOS clang 缺 macOS SDK, 报 ld: library 'System' not found)。
    干净 PATH (仅排除缓存影子目录) 中查找, 找不到再回退常见系统位置。"""
    path = os.environ.get("PATH", "")
    clean = ":".join(p for p in path.split(":")
                     if p and "ohos-sdk-ccache" not in p and "llvm-mingw-ccache" not in p)
    if clean:
        cand = shutil.which("cc", path=clean)
        if cand:
            return cand
    for cand in ("/usr/bin/cc", "/usr/local/bin/cc", "/opt/homebrew/bin/cc"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def make_clang_tmpdir():
    """为 clang 编译包装器创建临时目录: 放在 build 目录中的临时文件夹里。
    不创建临时文件夹的话，鸿蒙PC的 clang 可能会报
    'unable to make temporary file: Read-only file system'。
    build 目录不可写时回退系统临时目录。返回 (目录路径, 清理函数)。"""
    import tempfile
    try:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'build')
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(prefix=".ccache-clang-tmp-", dir=base)
    except OSError:
        d = tempfile.mkdtemp(prefix=".ccache-clang-tmp-")
    return d


def write_wrapper(dest, cache_tool, compiler, cc_cmd, clang_tmpdir):
    """生成 ELF 包装器 (C 源码经 stdin 注入 clang 编译, 不落临时文件)。
    clang 的 TMPDIR 显式指向 clang_tmpdir, 不受调用方 $TMPDIR 影响。
    编译失败时把 clang 的 stdout/stderr 直接打印到本脚本 stderr, 再抛出异常。"""
    c_src = WRAPPER_C % (c_escape(cache_tool), c_escape(compiler))
    env = dict(os.environ)
    env["TMPDIR"] = clang_tmpdir
    proc = subprocess.run([cc_cmd, "-pipe", "-x", "c", "-", "-o", dest],
                          input=c_src.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if out:
            sys.stderr.write("clang stdout: %s\n" % out)
        if err:
            sys.stderr.write("clang stderr: %s\n" % err)
        raise RuntimeError("clang 编译包装器失败 (exit=%d, cc=%s, 目标=%s)"
                           % (proc.returncode, cc_cmd, dest))
    os.chmod(dest, 0o755)


def symlink(src, dest):
    rm_if_exists(dest)
    os.symlink(src, dest)


def mirror_bin(real_bin, shadow_bin, cache_tool, is_mingw, cc_cmd, clang_tmpdir):
    """镜像 bin 目录: 编译器 → ELF 包装器, 三元组包装器按各自规则处理, 其余 → 符号链接。"""
    os.makedirs(shadow_bin, exist_ok=True)
    real_clang = os.path.realpath(os.path.join(real_bin, "clang"))
    real_clangxx = os.path.realpath(os.path.join(real_bin, "clang++"))

    for name in os.listdir(real_bin):
        src = os.path.join(real_bin, name)
        dst = os.path.join(shadow_bin, name)

        if is_mingw:
            # 三元组包装器 (x86_64-w64-mingw32-clang 等): 符号链接 → 影子内的副本
            if os.path.islink(src) and os.readlink(src) == MINGW_WRAPPER_SH:
                symlink(MINGW_WRAPPER_SH, dst)
                dot()
                continue
            # 共享包装脚本本身: 复制 (其内部 get_dir $0 需解析到影子目录)
            if name == MINGW_WRAPPER_SH:
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
                dot()
                continue
        else:
            # OHOS 三元组包装器: 复制而非符号链接 (内部 readlink -f $0 需落在影子目录)
            if OHOS_TRIPLE_RE.match(name):
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
                dot()
                continue

        compiler_arg = ""
        m = COMPILER_RE.match(name)
        if m:
            if m.group(4) is not None:
                # 版本化形式 (clang-15 ...): 与 clang/clang++ 同一二进制时用规范名
                resolved = os.path.realpath(src)
                if resolved == real_clang:
                    compiler_arg = os.path.join(real_bin, "clang")
                elif resolved == real_clangxx:
                    compiler_arg = os.path.join(real_bin, "clang++")
                else:
                    compiler_arg = src
            else:
                # 规范名: 直接用自身路径 (缓存工具可识别, 保持 clang/clang++ 语义)
                compiler_arg = src
        elif "clang" in name:
            # 改名/非常规命名的编译器兜底: realpath 命中真实编译器 → 替换
            resolved = os.path.realpath(src)
            if resolved == real_clang:
                compiler_arg = os.path.join(real_bin, "clang")
            elif resolved == real_clangxx:
                compiler_arg = os.path.join(real_bin, "clang++")

        if compiler_arg:
            write_wrapper(dst, cache_tool, compiler_arg, cc_cmd, clang_tmpdir)
        else:
            symlink(src, dst)
        dot()
    newline()


def mirror_toolchain(real_root, shadow_root, cache_tool, bin_rel, is_mingw,
                     cc_cmd, clang_tmpdir):
    """泛化镜像: 从工具链根出发, 沿 bin_rel 相对路径逐层下行。每一层先在本层把除
    路径外的兄弟条目尽可能在外层整体符号链接 (整个子树一个链接), 再进入下一层;
    最后一层 (bin) 的兄弟链接完后, 进入 bin 用 mirror_bin 做包装镜像。
    不区分 llvm-mingw / ohos-sdk — 只需给出根目录与 bin 相对路径。"""
    parts = [p for p in bin_rel.split("/") if p]
    if not parts:
        raise RuntimeError("bin_rel 不能为空")
    cur_real, cur_shadow = real_root, shadow_root
    path_so_far = ""
    for part in parts:
        # 1) 当前层: 除 part 外的兄弟条目, 尽可能在外层整体符号链接
        os.makedirs(cur_shadow, exist_ok=True)
        _write("[CCACHE]   镜像 %s (除 %s): "
               % (path_so_far.rstrip("/") or "顶层", part))
        for name in sorted(os.listdir(cur_real)):
            if name == part:
                continue
            symlink(os.path.join(cur_real, name), os.path.join(cur_shadow, name))
            dot()
        newline()
        # 2) 进入下一层
        cur_real = os.path.join(cur_real, part)
        cur_shadow = os.path.join(cur_shadow, part)
        path_so_far = part + "/"
    # 3) 最后一层 (bin): 包装镜像
    _write("[CCACHE]   镜像 %s: " % path_so_far.rstrip("/"))
    mirror_bin(cur_real, cur_shadow, cache_tool, is_mingw, cc_cmd, clang_tmpdir)


def main(argv):
    # 用法: <real_root> <shadow_root> <cache_tool> <bin_rel> [is_mingw] [fallback_cc]
    if len(argv) not in (5, 6, 7):
        sys.stderr.write(
            "用法: %s <real_root> <shadow_root> <cache_tool> <bin_rel> [is_mingw] [fallback_cc]\n"
            % argv[0])
        return 2
    real, shadow, cache_tool, bin_rel = argv[1:5]
    is_mingw = (argv[5] == "1") if len(argv) >= 6 else False
    fallback_cc = argv[6] if len(argv) == 7 else None
    real_bin = os.path.join(real, bin_rel)
    if not os.path.isdir(real_bin):
        sys.stderr.write("错误: 未找到 %s\n" % real_bin)
        return 1
    # 编译包装器的编译器: 优先 PATH (宿主 cc), 否则调用方兜底, 最后用 bin 自带 clang
    cc_cmd = wrapper_cc() or fallback_cc or os.path.join(real_bin, "clang")
    clang_tmpdir = make_clang_tmpdir()
    try:
        mirror_toolchain(real, shadow, cache_tool, bin_rel, is_mingw,
                         cc_cmd, clang_tmpdir)
    except Exception as exc:  # 含 clang 编译 ELF 包装器失败
        sys.stderr.write("错误: 镜像失败: %s\n" % exc)
        return 1
    finally:
        shutil.rmtree(clang_tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
