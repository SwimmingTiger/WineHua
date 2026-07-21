#!/usr/bin/env bash
BASE_DIR="$(dirname "$(realpath "$0")")"

# find uname-is-linux and ohos-sdk prefix from brew
BREW="$(command -v brew)"
if [ "$BREW" != "" ]; then
    LIBUNAME="$("$BREW" --prefix uname-is-linux)/lib/libuname.so"

    set -x
    # fix wine host tools missing libraries
    export LDFLAGS="-Wl,-rpath,$("$BREW" --prefix)/lib"
    { set +x; } 2>/dev/null

    if [ -z "$OHOS_SDK" ]; then
        set -x
        OHOS_SDK="$("$BREW" --prefix ohos-sdk)"
        { set +x; } 2>/dev/null
    fi
fi

# check ohos-sdk
if [ -z "$OHOS_SDK" ] || ! [ -d "$OHOS_SDK" ]; then
    echo "Please set the environment variable OHOS_SDK:"
    echo "    export OHOS_SDK=/path/to/ohos-sdk"
    echo "You can install ohos-sdk from Harmonybrew <https://harmonybrew.atomgit.com/>"
    echo "After finished the Harmonybrew setup, install ohos-sdk with this command:"
    echo "    brew install ohos-sdk"
    echo '    export OHOS_SDK="$(brew --prefix ohos-sdk)"'
    exit 1
fi
export OHOS_SDK

# check llvm-mingw
if [ -z "$LLVM_MINGW" ] || ! [ -d "$OHOS_SDK" ]; then
    echo "Please set the environment variable LLVM_MINGW:"
    echo "    export LLVM_MINGW=/path/to/llvm-mingw"
    echo "You can download it from https://github.com/SwimmingTiger/llvm-mingw/releases"
    exit 1
fi
export LLVM_MINGW

# check TOOL_HOME
if [ -z "$TOOL_HOME" ] || ! [ -d "$TOOL_HOME" ]; then
    echo "Please set the environment variable TOOL_HOME:"
    echo "    export TOOL_HOME=/path/to/command-line-tools"
    echo "You can download Linux x64 command-line-tools from https://developer.huawei.com/consumer/cn/download/command-line-tools-for-hmos"
    exit 1
fi
export TOOL_HOME

# We need the old cmake version from OHOS SDK.
# Harmonybrew cmake version is too high to some thirdparty projects.
set -x
export PATH="$OHOS_SDK/native/build-tools/cmake/bin:$OHOS_SDK/native/llvm/bin:$LLVM_MINGW/bin:$PATH"
{ set +x; } 2>/dev/null

# 替换 command-line-tools 中的所有 x64 ELF
$BASE_DIR/scripts/ohos-replace-x64-elf.py "$TOOL_HOME"
