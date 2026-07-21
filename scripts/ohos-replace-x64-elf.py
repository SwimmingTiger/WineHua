#!/usr/bin/env python3
"""Replace x86-64 ELF files under a directory with symlinks to host tools from PATH."""

import argparse
import concurrent.futures
import os
import shutil
import struct
import sys
import threading

MAX_WORKERS = min(os.cpu_count() or 1, 8)


class _ScanTracker:
    """Tracks in-flight scan tasks so main() waits until all are done.

    Each scan task calls add() before submitting and done() in its finally
    block.  The exit event is only signalled once the main thread has
    finished submitting all top-level entries (mark_submitted) AND every
    scan task (including recursively offloaded ones) has finished.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = 0
        self._all_submitted = False
        self._event = threading.Event()

    def add(self) -> None:
        with self._lock:
            self._pending += 1

    def done(self) -> None:
        should_set = False
        with self._lock:
            self._pending -= 1
            if self._pending == 0 and self._all_submitted:
                should_set = True
        if should_set:
            self._event.set()

    def mark_submitted(self) -> None:
        should_set = False
        with self._lock:
            self._all_submitted = True
            if self._pending == 0:
                should_set = True
        if should_set:
            self._event.set()

    def wait(self) -> None:
        self._event.wait()


def is_x64_executable(filepath: str) -> bool:
    """Check if file is an x86-64 ELF executable, excluding shared libraries.

    Distinguishes PIE executables (e_type=ET_DYN but with PT_INTERP)
    from true shared libraries (no PT_INTERP).
    """
    try:
        with open(filepath, "rb") as f:
            magic = f.read(4)
            if magic != b"\x7fELF" or f.read(1) != b"\x02":  # 64-bit
                return False

            # e_machine at offset 18: read from file offset 18
            f.seek(18)
            e_machine = struct.unpack("<H", f.read(2))[0]
            if e_machine != 62:  # EM_X86_64
                return False

            # Read program header location and scan for PT_INTERP
            f.seek(32)
            e_phoff = struct.unpack("<Q", f.read(8))[0]
            f.seek(54)
            e_phentsize = struct.unpack("<H", f.read(2))[0]
            e_phnum = struct.unpack("<H", f.read(2))[0]

            for i in range(e_phnum):
                f.seek(e_phoff + i * e_phentsize)
                phdr = f.read(e_phentsize)
                if len(phdr) < e_phentsize:
                    return False
                p_type = struct.unpack_from("<I", phdr, 0)[0]
                if p_type == 3:  # PT_INTERP
                    return True
    except OSError:
        return False

    return False


def replace_elf(filepath: str, dry_run: bool) -> None:
    basename = os.path.basename(filepath)
    host_tool = shutil.which(basename)
    if host_tool is None:
        print(f"SKIPPED: {filepath}  (no host tool '{basename}' found in PATH)",
              file=sys.stderr)
        return

    if dry_run:
        print(f"WOULD_REPLACE: {filepath} -> {host_tool}")
        return

    bak_path = filepath + ".x64-bak"
    try:
        os.rename(filepath, bak_path)
    except OSError as e:
        print(f"FAILED: {filepath}  (rename to .x64-bak): {e}", file=sys.stderr)
        return

    try:
        os.symlink(host_tool, filepath)
    except OSError as e:
        print(f"FAILED: {filepath}  (symlink to {host_tool}): {e}", file=sys.stderr)
        try:
            os.rename(bak_path, filepath)
        except OSError:
            pass
        return

    print(f"REPLACED: {filepath} -> {host_tool}")


def restore_elf(bak_path: str, dry_run: bool) -> None:
    """Restore a .x64-bak file back to its original path, removing the symlink."""
    if not bak_path.endswith(".x64-bak"):
        return
    orig_path = bak_path[:-len(".x64-bak")]

    if dry_run:
        print(f"WOULD_RESTORE: {bak_path} -> {orig_path}")
        return

    # Remove the symlink if it exists
    if os.path.islink(orig_path):
        try:
            os.unlink(orig_path)
        except OSError as e:
            print(f"FAILED: {orig_path}  (remove symlink): {e}", file=sys.stderr)
            return
    elif os.path.exists(orig_path):
        print(f"SKIPPED: {bak_path}  ({orig_path} exists but is not a symlink)",
              file=sys.stderr)
        return

    try:
        os.rename(bak_path, orig_path)
    except OSError as e:
        print(f"FAILED: {bak_path}  (rename back to {orig_path}): {e}", file=sys.stderr)
        return

    print(f"RESTORED: {orig_path}")


def walk_and_submit(path: str, pool: concurrent.futures.ThreadPoolExecutor,
                    dry_run: bool, restore: bool,
                    scan_pool: concurrent.futures.ThreadPoolExecutor,
                    scan_slots: threading.Semaphore,
                    tracker: _ScanTracker) -> None:
    try:
        if os.path.isfile(path):
            if restore:
                if path.endswith(".x64-bak") and not os.path.islink(path):
                    pool.submit(restore_elf, path, dry_run)
            else:
                if not os.path.islink(path) and is_x64_executable(path):
                    pool.submit(replace_elf, path, dry_run)
            return
        for dirpath, dirnames, filenames in os.walk(path):
            for dirname in list(dirnames):
                subdir = os.path.join(dirpath, dirname)
                if scan_slots.acquire(blocking=False):
                    dirnames.remove(dirname)
                    tracker.add()
                    scan_pool.submit(
                        walk_and_submit, subdir, pool, dry_run, restore,
                        scan_pool, scan_slots, tracker,
                    )
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.islink(filepath):
                    continue
                if restore:
                    if filename.endswith(".x64-bak"):
                        pool.submit(restore_elf, filepath, dry_run)
                else:
                    if filename.endswith(".x64-bak"):
                        continue
                    if not is_x64_executable(filepath):
                        continue
                    pool.submit(replace_elf, filepath, dry_run)
    finally:
        scan_slots.release()
        tracker.done()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace x86-64 ELF files with symlinks to host tools from PATH."
    )
    parser.add_argument("path", help="File or directory to process")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only list files that would be processed, without making changes"
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore .x64-bak files back to originals"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.path) and not os.path.lexists(args.path):
        print(f"Error: {args.path} is not a file or directory", file=sys.stderr)
        sys.exit(1)

    mode = " (DRY RUN)" if args.dry_run else ""
    action = "Restoring" if args.restore else "Scanning for x86-64 ELF files"
    print(f"{action} in {args.path}{mode}")

    if not os.path.isdir(args.path):
        if args.restore:
            restore_elf(args.path, args.dry_run)
        else:
            if not os.path.islink(args.path) and is_x64_executable(args.path):
                replace_elf(args.path, args.dry_run)
        print("Done.")
        return

    scan_slots = threading.Semaphore(MAX_WORKERS)
    tracker = _ScanTracker()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as scan_pool:
            for entry in os.listdir(args.path):
                entry_path = os.path.join(args.path, entry)
                scan_slots.acquire()
                tracker.add()
                scan_pool.submit(
                    walk_and_submit, entry_path, pool, args.dry_run, args.restore,
                    scan_pool, scan_slots, tracker,
                )
            tracker.mark_submitted()
            tracker.wait()

    print("Done.")


if __name__ == "__main__":
    main()
