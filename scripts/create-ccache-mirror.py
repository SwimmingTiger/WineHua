#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create-ccache-mirror.py — 用符号链接镜像工具链 bin 目录, 把编译器替换为调用缓存
工具 (sccache/ccache) 的包装脚本。

由 scripts/ccache.sh 调用: bash 逐条 fork cp/ln/basename/python3 太慢 (llvm-mingw
bin 约 700 个条目), 本脚本用 Python 单进程完成全部镜像, 行为与原 bash 实现完全一致
(包装脚本内容 / 符号链接 / 三元组包装器复制 / cc/c++/gcc/g++ 真实编译器解析 / 进度点)。

用法:
    create-ccache-mirror.py ohos  <real_sdk>    <shadow_root> <cache_tool>
    create-ccache-mirror.py mingw <real_mingw>  <shadow_root> <cache_tool>

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


def x_mark():
    _write("x")


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
    """为 clang 编译包装器创建临时目录: 放在脚本所在目录旁 (仓库所在文件系统),
    避开调用方 $TMPDIR 可能为不支持 clang 临时文件机制 (如 O_TMPFILE) 的文件系统,
    否则 clang 会报 'unable to make temporary file: Read-only file system'。
    脚本目录不可写时回退系统临时目录。返回 (目录路径, 清理函数)。"""
    import tempfile
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        # 清理上次异常退出可能遗留的临时目录
        for old in glob.glob(os.path.join(base, ".ccache-clang-tmp-*")):
            shutil.rmtree(old, ignore_errors=True)
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


def which_clean(name, blocked):
    """在不含影子 bin 的干净 PATH 上解析系统真实编译器 (cc/gcc/c++/g++)。
    排除任何含影子目录特征 (ohos-sdk-ccache / llvm-mingw-ccache) 的 PATH 项 —
    不止当前影子 — 避免把历史/残留影子的包装解析成"真实"编译器。"""
    path = os.environ.get("PATH", "")
    clean = ":".join(p for p in path.split(":")
                     if p and p not in blocked
                     and "ohos-sdk-ccache" not in p and "llvm-mingw-ccache" not in p)
    if not clean:
        return None
    return shutil.which(name, path=clean)


def mirror_ohos_levels(real_sdk, shadow):
    """镜像 llvm(除 bin) / native(除 llvm) / SDK 顶层(除 native) 三级目录。"""
    levels = (
        (os.path.join(real_sdk, "native", "llvm"), os.path.join(shadow, "native", "llvm"), "bin"),
        (os.path.join(real_sdk, "native"), os.path.join(shadow, "native"), "llvm"),
        (real_sdk, shadow, "native"),
    )
    for src_dir, dst_dir, skip in levels:
        os.makedirs(dst_dir, exist_ok=True)
        for name in os.listdir(src_dir):
            if name == skip:
                continue
            symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))
            dot()
    newline()


def mirror_ohos(real_sdk, shadow, cache_tool, fallback_cc=None, clang_tmpdir=None):
    real_bin = os.path.join(real_sdk, "native", "llvm", "bin")
    shadow_bin = os.path.join(shadow, "native", "llvm", "bin")
    # 编译包装器的 clang: 优先 PATH (宿主), 否则 OHOS SDK clang
    cc_cmd = wrapper_cc() or fallback_cc or os.path.join(real_bin, "clang")

    _write("[CCACHE]   镜像 llvm/bin: ")
    mirror_bin(real_bin, shadow_bin, cache_tool, is_mingw=False, cc_cmd=cc_cmd,
               clang_tmpdir=clang_tmpdir)

    # 补充按名字查找的编译器入口: 包装到系统真实的 cc/c++/gcc/g++
    _write("[CCACHE]   补充按名查找入口 (cc/c++/gcc/g++): ")
    blocked = {real_bin, shadow_bin}
    for name in ("cc", "c++", "gcc", "g++"):
        compiler = which_clean(name, blocked)
        if compiler:
            write_wrapper(os.path.join(shadow_bin, name), cache_tool, compiler, cc_cmd,
                          clang_tmpdir)
            dot()
        else:
            x_mark()
    newline()

    _write("[CCACHE]   镜像 llvm / native / SDK 顶层: ")
    mirror_ohos_levels(real_sdk, shadow)


def mirror_mingw(real_mingw, shadow, cache_tool, fallback_cc=None, clang_tmpdir=None):
    # 编译包装器的 clang: 优先 PATH (宿主), 否则 OHOS SDK clang (由调用方传入)
    cc_cmd = wrapper_cc() or fallback_cc
    if not cc_cmd:
        raise RuntimeError("PATH 中无 clang 且未提供 OHOS SDK clang 兜底, 无法编译包装器")
    _write("[CCACHE]   镜像 bin: ")
    mirror_bin(os.path.join(real_mingw, "bin"), os.path.join(shadow, "bin"),
               cache_tool, is_mingw=True, cc_cmd=cc_cmd, clang_tmpdir=clang_tmpdir)


def main(argv):
    # 用法: ohos|mingw <real> <shadow> <cache_tool> [fallback_cc]
    if len(argv) not in (5, 6) or argv[1] not in ("ohos", "mingw"):
        sys.stderr.write(
            "用法: %s ohos|mingw <real> <shadow_root> <cache_tool> [fallback_cc]\n" % argv[0])
        return 2
    mode, real, shadow, cache_tool = argv[1:5]
    fallback_cc = argv[5] if len(argv) == 6 else None
    real_bin = (os.path.join(real, "native", "llvm", "bin") if mode == "ohos"
                else os.path.join(real, "bin"))
    if not os.path.isdir(real_bin):
        sys.stderr.write("错误: 未找到 %s\n" % real_bin)
        return 1
    clang_tmpdir = make_clang_tmpdir()
    try:
        if mode == "ohos":
            mirror_ohos(real, shadow, cache_tool, fallback_cc, clang_tmpdir)
        else:
            mirror_mingw(real, shadow, cache_tool, fallback_cc, clang_tmpdir)
    except Exception as exc:  # 含 clang 编译 ELF 包装器失败
        sys.stderr.write("错误: 镜像失败: %s\n" % exc)
        return 1
    finally:
        shutil.rmtree(clang_tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
