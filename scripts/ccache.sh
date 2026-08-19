#!/usr/bin/env bash
# 构建缓存启用逻辑 (sccache / ccache) — source 本文件即立即启用构建缓存, 无需再调用函数。
# 用法:
#   * build-on-ohos.sh: source "$(dirname "$0")/scripts/ccache.sh" (在 OHOS_SDK 确定后)
#   * 用户手动启用:      source /path/to/WineHua/scripts/ccache.sh
# source 后的效果:
#   * 检测缓存工具 (优先级: 用户自定义 CCACHE_WRAPPER > sccache > ccache);
#   * 用符号链接构造影子 OHOS_SDK, 把编译器替换为调用缓存工具的包装脚本, 并切换
#     OHOS_SDK 指向影子 — 之后所有 $OHOS_SDK 绝对路径的编译器调用都会命中缓存。
# 依赖调用方提供: OHOS_SDK (真实 SDK 路径, 不自动推导); 影子 SDK 存放在
# $TMPDIR/ohos-sdk-ccache (TMPDIR 未设置时默认 /tmp)。
# 幂等: 重复 source 不会重建/叠加包装器 — 影子 SDK 内记录来源 stamp (真实 SDK +
# 缓存工具), 已存在且缓存工具未变时直接复用; 缓存工具变化时才基于 stamp 重建。
# 环境变量:
#   NO_CCACHE=1     禁用构建缓存 (优先级最高)
#   CCACHE_WRAPPER  用户自定义缓存工具 (优先级最高; 如 CCACHE_WRAPPER=ccache)
#   CCACHE_SDK_DIR  影子 SDK 目录 (默认 $TMPDIR/ohos-sdk-ccache)

# ── 1) 检测缓存工具 (source 时立即执行) ──
if [ "${NO_CCACHE:-0}" = "1" ]; then
    unset CACHE_TOOL CCACHE_WRAPPER
    echo "[CCACHE] 检测到 NO_CCACHE=1, 构建缓存已禁用"
    echo "[CCACHE] 提示: 如需重新启用构建缓存, 请取消 NO_CCACHE 环境变量后重试 (如: unset NO_CCACHE)"
else
    CACHE_TOOL=""
    if [ -n "${CCACHE_WRAPPER:-}" ]; then
        # 用户自定义 CCACHE_WRAPPER 具有最高优先级: 直接使用, 不覆盖、不自动检测
        CACHE_TOOL="$CCACHE_WRAPPER"
        if [[ "$CACHE_TOOL" != */* ]]; then
            # 裸命令名 (如 CCACHE_WRAPPER=ccache) 解析为绝对路径, 便于包装脚本
            CACHE_TOOL="$(command -v "$CCACHE_WRAPPER" 2>/dev/null || echo "$CCACHE_WRAPPER")"
        fi
        echo "[CCACHE] 使用用户自定义 CCACHE_WRAPPER: $CCACHE_WRAPPER (禁用: NO_CCACHE=1)"
    else
        CACHE_TOOL="$(command -v sccache 2>/dev/null)"
        if [ -z "$CACHE_TOOL" ]; then
            CACHE_TOOL="$(command -v ccache 2>/dev/null)"
        fi
    fi
    if [ -n "$CACHE_TOOL" ]; then
        echo "[CCACHE] 已启用 $(basename "$CACHE_TOOL") 构建缓存: $CACHE_TOOL (禁用: NO_CCACHE=1)"
    else
        unset CCACHE_WRAPPER
        echo "[CCACHE] 未检测到 sccache / ccache, 构建缓存未启用"
        echo "[CCACHE] 提示: 可通过 brew install sccache / apt install ccache 等方式安装构建缓存工具来加速后续构建"
    fi
fi

# ── 2) 影子 OHOS_SDK ──
# 原理: 把整个 SDK 镜像成符号链接目录 ($CCACHE_SDK_DIR, 默认 $BUILD_DIR/ohos-sdk-ccache,
# 位于 HMDFS 之外), 仅把编译器替换为调用缓存工具的包装脚本。之后 meson 交叉文件 /
# wine $CLANG / cmake toolchain 里的 $OHOS_SDK 绝对路径全部命中缓存, 无需改任何脚本。
# 启发式探测 (面向未来 SDK 变化, 不遗漏):
#   * 真实编译器 = clang / clang++ 符号链接最终指向的二进制 (realpath), 版本号变化
#     (clang-15 → clang-16 / clang-15.0.6) 自动适配;
#   * 名字模式覆盖 clang / clang++ / clang-cl / clang-cpp 及其版本化形式, 与
#     clang-format / clang-tidy / clangd 等非编译器工具区分开;
#   * 名字含 "clang" 的其他条目做 realpath 比对, 命中真实编译器同样替换 (防改名遗漏);
#   * 三元组包装器 (*-unknown-linux-ohos-clang) 整体复制 — 其内部 readlink -f \$0
#     必须落在影子目录才会 exec 影子 clang;
#   * 其余所有文件/文件夹一律符号链接 — 镜像按目录实际内容遍历, 新增内容自动纳入;
#   * 补充按名字查找的编译器入口 cc/c++/gcc/g++ → 缓存工具 + 系统真实的同名编译器
#     (以不含影子 bin / 真实 SDK bin 的干净 PATH 解析), 使不读 CC/CXX 环境变量、
#     直接按名字找编译器的构建也命中缓存;
#   * 额外构造 llvm-mingw 影子 ($TMPDIR/llvm-mingw-ccache) 并 export LLVM_MINGW
#     指向影子, 使 wine 的 PE 交叉编译 ($LLVM_MINGW/bin/clang) 也命中缓存; 三元组
#     包装器经复制的 clang-target-wrapper.sh 转投影子 clang。
realpath_of() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || echo "$1"
}

# 把给定目录放到 PATH 最前 (去重): 先移除 PATH 中已有的这些目录项, 再按参数顺序前置。
# 用于确保影子 bin 一定在 PATH 开头, 让按名字查找的编译器调用 (如 clang/cc) 都命中缓存。
prepend_path_front() {
    local old_ifs="$IFS" seg keep new_path="" front=""
    IFS=:
    for seg in $PATH; do
        [ -n "$seg" ] || continue
        keep=1
        for d in "$@"; do
            [ "$seg" = "$d" ] && { keep=0; break; }
        done
        [ "$keep" = "1" ] && new_path="${new_path:+$new_path:}$seg"
    done
    IFS="$old_ifs"
    for d in "$@"; do
        front="${front:+$front:}$d"
    done
    export PATH="$front${new_path:+:$new_path}"
}

ccache_setup_shadow_sdk() {
    # $1 = "clean" 时跳过 (./build-on-ohos.sh clean 不需要影子 SDK)
    local clean_guard="${1:-}"
    if [ -z "${CACHE_TOOL:-}" ] || [ "$clean_guard" = "clean" ]; then
        return 0
    fi

    # 真实 SDK 必须由调用方提供 (build-on-ohos.sh 或用户 shell 设置 OHOS_SDK), 不自动推导;
    # 缺失/无效时直接报错
    if [ -z "${OHOS_SDK:-}" ] || ! [ -d "$OHOS_SDK" ]; then
        echo "[CCACHE] 错误: 未找到有效的 OHOS_SDK (当前值: ${OHOS_SDK:-<未设置>}), 无法启用构建缓存" >&2
        echo "[CCACHE] 请先设置 OHOS_SDK 环境变量 (如: export OHOS_SDK=/path/to/ohos-sdk)" >&2
        return 1
    fi
    local real_sdk="$OHOS_SDK"
    local cache_tool="$CACHE_TOOL"
    # 影子 SDK 直接放在 TMPDIR (未设置时默认 /tmp)
    local TMPDIR="${TMPDIR:-/tmp}"
    local shadow="${CCACHE_SDK_DIR:-$TMPDIR/ohos-sdk-ccache}"

    # 幂等: 若 OHOS_SDK 已被上一轮 source 切换成影子, 从影子内的 stamp 恢复真实 SDK,
    # 避免把影子当真实 SDK 再镜像一层 (或重建出损坏的影子)
    local stamp="$shadow/.ccache-stamp"
    if [ -f "$real_sdk/.ccache-stamp" ]; then
        real_sdk="$(sed -n 1p "$real_sdk/.ccache-stamp")"
    fi
    local real_bin="$real_sdk/native/llvm/bin"
    if [ -z "$real_sdk" ] || ! [ -d "$real_bin" ]; then
        echo "[CCACHE] 错误: 未找到有效的 OHOS_SDK (当前值: ${OHOS_SDK:-<未设置>}), 无法启用构建缓存" >&2
        echo "[CCACHE] 请先设置 OHOS_SDK 环境变量 (如: export OHOS_SDK=/path/to/ohos-sdk)" >&2
        return 1
    fi

    # 幂等复用: 影子已存在, 且来源 (真实 SDK) 与缓存工具均未变化 → 不重建
    if [ -f "$stamp" ] && [ -f "$shadow/native/llvm/bin/clang" ]; then
        local stored_real stored_tool
        stored_real="$(sed -n 1p "$stamp")"
        stored_tool="$(sed -n 2p "$stamp")"
        if [ "$stored_real" = "$real_sdk" ] && [ "$stored_tool" = "$cache_tool" ]; then
            # 影子 bin 置顶 (去重), 保证按名字查找的编译器调用命中缓存
            prepend_path_front "$shadow/native/llvm/bin" "$real_bin"
            export OHOS_SDK="$shadow"
            echo "[CCACHE] 影子 OHOS_SDK 已就绪 (复用): $shadow (禁用: NO_CCACHE=1)"
            ccache_setup_mingw_shadow "$cache_tool"
            return 0
        fi
        # 来源或工具变化 → 走下方重建 (真实 SDK 取 OHOS_SDK; 若其本身是影子,
        # 上文已从影子自身的 stamp 恢复出真实 SDK)
    fi

    # 探测真实编译器 (clang/clang++ 最终指向的二进制), 版本变化自动适配
    local real_clang real_clangxx
    real_clang="$(realpath_of "$real_bin/clang")"
    real_clangxx="$(realpath_of "$real_bin/clang++")"

    rm -rf "$shadow"
    mkdir -p "$shadow/native/llvm/bin"

    # ── 生成进度: 每处理一个条目输出一个 '.' ──
    printf '[CCACHE] 正在生成影子 SDK: %s\n' "$shadow"
    printf '[CCACHE]   镜像 llvm/bin: '
    local f name resolved compiler_arg
    for f in "$real_bin"/*; do
        name="$(basename "$f")"
        compiler_arg=""
        case "$name" in
            clang|clang++|clang-cl|clang-cpp)
                # 规范名: 直接用自身路径 (缓存工具可识别, 保持 clang/clang++ 语义)
                compiler_arg="$f"
                ;;
            clang-[0-9]*|clang++-[0-9]*|clang-cl-[0-9]*|clang-cpp-[0-9]*)
                # 版本化形式 (clang-15 / clang-15.0.6 ...): 与 clang/clang++ 同一
                # 二进制时改用规范名 (识别更稳), 否则用自身路径
                resolved="$(realpath_of "$f")"
                if [ "$resolved" = "$real_clang" ]; then
                    compiler_arg="$real_bin/clang"
                elif [ "$resolved" = "$real_clangxx" ]; then
                    compiler_arg="$real_bin/clang++"
                else
                    compiler_arg="$f"
                fi
                ;;
            *-unknown-linux-ohos-clang|*-unknown-linux-ohos-clang++)
                # 三元组包装器: 复制而非符号链接 (readlink -f \$0 需落在影子目录)
                cp -f "$f" "$shadow/native/llvm/bin/$name"
                chmod +x "$shadow/native/llvm/bin/$name"
                printf '.'
                continue
                ;;
            *)
                if [[ "$name" == *clang* ]]; then
                    # 改名/非常规命名的编译器兜底: realpath 命中真实编译器 → 替换
                    resolved="$(realpath_of "$f")"
                    if [ "$resolved" = "$real_clang" ]; then
                        compiler_arg="$real_bin/clang"
                    elif [ "$resolved" = "$real_clangxx" ]; then
                        compiler_arg="$real_bin/clang++"
                    fi
                fi
                ;;
        esac
        if [ -n "$compiler_arg" ]; then
            # 包装脚本: exec <缓存工具> <真实编译器> "$@"
            cat > "$shadow/native/llvm/bin/$name" <<EOF
#!/bin/sh
exec "$cache_tool" "$compiler_arg" "\$@"
EOF
            chmod +x "$shadow/native/llvm/bin/$name"
        else
            ln -sf "$f" "$shadow/native/llvm/bin/$name"
        fi
        printf '.'
    done
    printf '\n'

    # 补充按名字查找的编译器入口: 包装到系统真实的 cc/c++/gcc/g++。用不含影子 bin
    # 和真实 SDK bin 的干净 PATH 解析, 避免解析到我们自己的包装或 SDK clang。
    # 系统缺少某个编译器时跳过 (进度输出 'x')。
    local clean_path="" seg real_cc real_cxx real_gcc real_gxx
    local old_ifs="$IFS"
    IFS=:
    for seg in $PATH; do
        [ -n "$seg" ] || continue
        [ "$seg" = "$shadow/native/llvm/bin" ] && continue
        [ "$seg" = "$real_bin" ] && continue
        clean_path="${clean_path:+$clean_path:}$seg"
    done
    IFS="$old_ifs"
    real_cc="$(PATH="$clean_path" command -v cc 2>/dev/null || true)"
    real_cxx="$(PATH="$clean_path" command -v c++ 2>/dev/null || true)"
    real_gcc="$(PATH="$clean_path" command -v gcc 2>/dev/null || true)"
    real_gxx="$(PATH="$clean_path" command -v g++ 2>/dev/null || true)"

    printf '[CCACHE]   补充按名查找入口 (cc/c++/gcc/g++): '
    local cc_name cc_compiler
    for cc_name in cc c++ gcc g++; do
        case "$cc_name" in
            cc)  cc_compiler="$real_cc" ;;
            c++) cc_compiler="$real_cxx" ;;
            gcc) cc_compiler="$real_gcc" ;;
            g++) cc_compiler="$real_gxx" ;;
        esac
        if [ -n "$cc_compiler" ]; then
            cat > "$shadow/native/llvm/bin/$cc_name" <<EOF
#!/bin/sh
exec "$cache_tool" "$cc_compiler" "\$@"
EOF
            chmod +x "$shadow/native/llvm/bin/$cc_name"
            printf '.'
        else
            printf 'x'
        fi
    done
    printf '\n'

    # 镜像 llvm 下除 bin 外的内容 (lib/include/... 新增内容自动纳入)
    printf '[CCACHE]   镜像 llvm / native / SDK 顶层: '
    for e in "$real_sdk/native/llvm"/*; do
        name="$(basename "$e")"
        [ "$name" = "bin" ] && continue
        ln -sfn "$e" "$shadow/native/llvm/$name"
        printf '.'
    done
    # 镜像 native 下除 llvm 外的内容
    for e in "$real_sdk/native"/*; do
        name="$(basename "$e")"
        [ "$name" = "llvm" ] && continue
        ln -sfn "$e" "$shadow/native/$name"
        printf '.'
    done
    # 镜像 SDK 顶层除 native 外的内容
    for e in "$real_sdk"/*; do
        name="$(basename "$e")"
        [ "$name" = "native" ] && continue
        ln -sfn "$e" "$shadow/$name"
        printf '.'
    done
    printf '\n'

    # 记录来源 (真实 SDK + 缓存工具), 供下次 source 幂等复用
    printf '%s\n%s\n' "$real_sdk" "$cache_tool" > "$stamp"

    # 影子 bin 置顶 (去重), 真实 llvm/bin 兜底, 保证按名字查找的编译器调用命中缓存
    prepend_path_front "$shadow/native/llvm/bin" "$real_bin"
    export OHOS_SDK="$shadow"
    echo "[CCACHE] 影子 OHOS_SDK 就绪: $shadow (禁用: NO_CCACHE=1)"

    # llvm-mingw 影子 (PE 交叉编译, 如 wine 的 --with-mingw)
    ccache_setup_mingw_shadow "$cache_tool"
}

# 构造 llvm-mingw 影子 (仅编译器 → 缓存工具包装脚本, 其余 → 符号链接), 然后
# export LLVM_MINGW 指向影子, 使 $LLVM_MINGW/bin/clang 等绝对路径的 PE 交叉编译
# 也命中缓存。注意: mingw 影子不进 PATH (避免与 OHOS 影子的 clang 抢占按名查找)。
# 三元组包装器 (x86_64-w64-mingw32-clang 等) 是符号链接到共享的
# clang-target-wrapper.sh, 该脚本内部 get_dir \$0 会解析符号链接定位 clang —
# 因此 clang-target-wrapper.sh 需复制进影子, 三元组符号链接重建为指向影子内副本,
# 使 \$0 解析落在影子目录, 进而 exec 影子 clang (命中缓存)。
ccache_setup_mingw_shadow() {
    local cache_tool="${1:-}"
    if [ -z "${CACHE_TOOL:-}" ] || [ -z "${LLVM_MINGW:-}" ] || ! [ -d "$LLVM_MINGW" ]; then
        return 0
    fi

    # 幂等: LLVM_MINGW 可能已是影子 → 从影子 stamp 恢复真实路径
    local real_mingw="$LLVM_MINGW"
    local TMPDIR="${TMPDIR:-/tmp}"
    local shadow="${LLVM_MINGW_CCACHE_DIR:-$TMPDIR/llvm-mingw-ccache}"
    local stamp="$shadow/.ccache-stamp"
    if [ -f "$real_mingw/.ccache-stamp" ]; then
        real_mingw="$(sed -n 1p "$real_mingw/.ccache-stamp")"
    fi
    local real_bin="$real_mingw/bin"
    if ! [ -d "$real_bin" ]; then
        echo "[CCACHE] 警告: 未找到 $real_bin, 跳过 llvm-mingw 影子" >&2
        return 0
    fi

    # 幂等复用: 影子已存在且来源 (真实 LLVM_MINGW) 与缓存工具均未变化 → 不重建
    if [ -f "$stamp" ] && [ -f "$shadow/bin/clang" ]; then
        local stored_real stored_tool
        stored_real="$(sed -n 1p "$stamp")"
        stored_tool="$(sed -n 2p "$stamp")"
        if [ "$stored_real" = "$real_mingw" ] && [ "$stored_tool" = "$cache_tool" ]; then
            export LLVM_MINGW="$shadow"
            echo "[CCACHE] llvm-mingw 影子就绪 (复用): $shadow"
            return 0
        fi
    fi

    printf '[CCACHE] 正在生成 llvm-mingw 影子: %s\n' "$shadow"
    rm -rf "$shadow"
    mkdir -p "$shadow/bin"

    local real_clang real_clangxx
    real_clang="$(realpath_of "$real_bin/clang")"
    real_clangxx="$(realpath_of "$real_bin/clang++")"

    printf '[CCACHE]   镜像 bin: '
    local f name resolved compiler_arg
    for f in "$real_bin"/*; do
        name="$(basename "$f")"
        # 三元组包装器: 符号链接 → 影子内的 clang-target-wrapper.sh 副本
        if [ -L "$f" ] && [ "$(readlink "$f")" = "clang-target-wrapper.sh" ]; then
            ln -sf clang-target-wrapper.sh "$shadow/bin/$name"
            printf '.'
            continue
        fi
        case "$name" in
            clang-target-wrapper.sh)
                # 复制: get_dir \$0 需解析到影子目录, 才会 exec 影子 clang
                cp -f "$f" "$shadow/bin/$name"
                chmod +x "$shadow/bin/$name"
                printf '.'
                continue
                ;;
        esac
        compiler_arg=""
        case "$name" in
            clang|clang++|clang-cl|clang-cpp)
                # 规范名: 直接用自身路径 (缓存工具可识别, 保持 clang/clang++ 语义)
                compiler_arg="$f"
                ;;
            clang-[0-9]*|clang++-[0-9]*|clang-cl-[0-9]*|clang-cpp-[0-9]*)
                # 版本化形式 (clang-22 ...): 与 clang/clang++ 同一二进制时用规范名
                resolved="$(realpath_of "$f")"
                if [ "$resolved" = "$real_clang" ]; then
                    compiler_arg="$real_bin/clang"
                elif [ "$resolved" = "$real_clangxx" ]; then
                    compiler_arg="$real_bin/clang++"
                else
                    compiler_arg="$f"
                fi
                ;;
            *)
                if [[ "$name" == *clang* ]]; then
                    # 改名/非常规命名的编译器兜底: realpath 命中真实编译器 → 替换
                    resolved="$(realpath_of "$f")"
                    if [ "$resolved" = "$real_clang" ]; then
                        compiler_arg="$real_bin/clang"
                    elif [ "$resolved" = "$real_clangxx" ]; then
                        compiler_arg="$real_bin/clang++"
                    fi
                fi
                ;;
        esac
        if [ -n "$compiler_arg" ]; then
            cat > "$shadow/bin/$name" <<EOF
#!/bin/sh
exec "$cache_tool" "$compiler_arg" "\$@"
EOF
            chmod +x "$shadow/bin/$name"
        else
            ln -sf "$f" "$shadow/bin/$name"
        fi
        printf '.'
    done
    printf '\n'

    # 记录来源 (真实 LLVM_MINGW + 缓存工具), 供下次 source 幂等复用
    printf '%s\n%s\n' "$real_mingw" "$cache_tool" > "$stamp"
    export LLVM_MINGW="$shadow"
    echo "[CCACHE] llvm-mingw 影子就绪: $shadow (禁用: NO_CCACHE=1)"
}

# ── 3) source 时立即启用 (不依赖调用方再调函数) ──
# "$1" 为调用方 (build-on-ohos.sh) 的位置参数, clean 时跳过
ccache_setup_shadow_sdk "$1"
