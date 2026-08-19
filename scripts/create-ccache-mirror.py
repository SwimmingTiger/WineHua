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
import os
import re
import shutil
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


def write_wrapper(dest, cache_tool, compiler):
    """生成包装脚本: exec <缓存工具> <真实编译器> "$@" (与原 bash 版逐字节一致)。"""
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (cache_tool, compiler))
    os.chmod(dest, 0o755)


def symlink(src, dest):
    rm_if_exists(dest)
    os.symlink(src, dest)


def mirror_bin(real_bin, shadow_bin, cache_tool, is_mingw):
    """镜像 bin 目录: 编译器 → 包装脚本, 三元组包装器按各自规则处理, 其余 → 符号链接。"""
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
            write_wrapper(dst, cache_tool, compiler_arg)
        else:
            symlink(src, dst)
        dot()
    newline()


def which_clean(name, blocked):
    """在不含影子/真实 bin 的干净 PATH 上解析系统真实编译器 (cc/gcc/c++/g++)。"""
    path = os.environ.get("PATH", "")
    clean = ":".join(p for p in path.split(":") if p and p not in blocked)
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


def mirror_ohos(real_sdk, shadow, cache_tool):
    real_bin = os.path.join(real_sdk, "native", "llvm", "bin")
    shadow_bin = os.path.join(shadow, "native", "llvm", "bin")

    _write("[CCACHE]   镜像 llvm/bin: ")
    mirror_bin(real_bin, shadow_bin, cache_tool, is_mingw=False)

    # 补充按名字查找的编译器入口: 包装到系统真实的 cc/c++/gcc/g++
    _write("[CCACHE]   补充按名查找入口 (cc/c++/gcc/g++): ")
    blocked = {real_bin, shadow_bin}
    for name in ("cc", "c++", "gcc", "g++"):
        compiler = which_clean(name, blocked)
        if compiler:
            write_wrapper(os.path.join(shadow_bin, name), cache_tool, compiler)
            dot()
        else:
            x_mark()
    newline()

    _write("[CCACHE]   镜像 llvm / native / SDK 顶层: ")
    mirror_ohos_levels(real_sdk, shadow)


def mirror_mingw(real_mingw, shadow, cache_tool):
    _write("[CCACHE]   镜像 bin: ")
    mirror_bin(os.path.join(real_mingw, "bin"), os.path.join(shadow, "bin"),
               cache_tool, is_mingw=True)


def main(argv):
    if len(argv) != 5 or argv[1] not in ("ohos", "mingw"):
        sys.stderr.write(
            "用法: %s ohos|mingw <real> <shadow_root> <cache_tool>\n" % argv[0])
        return 2
    mode, real, shadow, cache_tool = argv[1:5]
    real_bin = (os.path.join(real, "native", "llvm", "bin") if mode == "ohos"
                else os.path.join(real, "bin"))
    if not os.path.isdir(real_bin):
        sys.stderr.write("错误: 未找到 %s\n" % real_bin)
        return 1
    if mode == "ohos":
        mirror_ohos(real, shadow, cache_tool)
    else:
        mirror_mingw(real, shadow, cache_tool)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
