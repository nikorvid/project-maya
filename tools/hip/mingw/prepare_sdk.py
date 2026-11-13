#!/usr/bin/env python3
"""Prepare AMD's ROCm SDK (the .venv wheels) for a MinGW-target clang build of Maya's HIP engine.

Two edits, both inside .venv (nothing system-wide), both idempotent:

1. HIP's headers branch on the compiler vendor where the assumption is really about the
   data model: amd_detail/amd_warp_functions.h guards LP64 `long` shuffles with
   `#ifndef _MSC_VER`, so a MinGW-target clang (LLP64, no _MSC_VER) trips a static_assert
   at sizeof(long) == 4.  The correct condition is the ABI, not the vendor: Windows is
   LLP64 whatever the compiler.  Patched to `#if !defined(_MSC_VER) && !defined(_WIN32)`;
   the original is kept beside it as *.h.orig.

2. CMake hardcodes --rtlib=compiler-rt on HIP link lines, and ROCm's clang ships no MinGW
   compiler-rt builtins (libclang_rt.builtins.a for x86_64-w64-windows-gnu).  MinGW's
   libgcc.a provides the same symbols, so it is copied under the name clang looks for.

    python tools/hip/mingw/prepare_sdk.py            (uses .venv's ROCm SDK)
    python tools/hip/mingw/prepare_sdk.py --check     report only, change nothing
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROCM = ROOT / ".venv" / "Lib" / "site-packages" / "_rocm_sdk_devel"
DEFAULT_UCRT = Path("C:/msys64/ucrt64")

# the header whose vendor check is really a data-model check (8 overloads: long, double, ...)
WARP_HEADER = Path("include/hip/amd_detail/amd_warp_functions.h")
VENDOR_GUARD = "#ifndef _MSC_VER"
ABI_GUARD = "#if !defined(_MSC_VER) && !defined(_WIN32)"

# HIP's own size_t is unsigned long unless the compiler is MSVC.  On Windows that is 32-bit
# (LLP64), while the size_t the rest of these headers use is 64-bit, so make_index_sequence
# and every HIP_vector_type<...> instantiated from it disagree; on a MinGW clang the
# template argument deduces as 'unsigned long' vs 'size_t (aka unsigned long long)' and
# vector types stop compiling.  The condition is the data model again, not the vendor.
DEFINES_HEADER = Path("include/hip/amd_detail/host_defines.h")
SIZE_T_OLD = "#if defined(_MSC_VER)\ntypedef unsigned long long size_t;\n#else\ntypedef unsigned long size_t;\n#endif"
SIZE_T_NEW = "#if defined(_WIN32)\ntypedef unsigned long long size_t;\n#else\ntypedef unsigned long size_t;\n#endif"

# ROCm's CMake package asks clang for "-print-libgcc-file-name --rtlib=compiler-rt" and adds
# the result to hip::host/hip::device.  For a MinGW-target clang that is
# clang_rt.builtins-x86_64.lib, the MSVC-flavoured runtime, which wants MSVC CRT symbols
# (__security_check_cookie, __security_cookie, _fltused) that a MinGW link does not have.
# A MinGW build uses libgcc; MSVC builds keep upstream's behaviour unchanged.
HIP_CMAKE = Path("lib/cmake/hip/hip-config-amd.cmake")
HIP_CMAKE_OPEN_OLD = ("# Use HIP_CXX option -print-libgcc-file-name --rtlib=compiler-rt\n"
                      "# To fetch the compiler rt library file name.\nexecute_process(")
HIP_CMAKE_OPEN_NEW = ("# Use HIP_CXX option -print-libgcc-file-name --rtlib=compiler-rt\n"
                      "# To fetch the compiler rt library file name.\nif(MSVC)\nexecute_process(")
HIP_CMAKE_CLOSE_OLD = "endif() # CLANGRT_Error Check"
HIP_CMAKE_CLOSE_NEW = ("endif() # CLANGRT_Error Check\n"
                       "endif() # MSVC - a MinGW-target clang links libgcc, not compiler-rt")

PATCHES = [
    (WARP_HEADER, VENDOR_GUARD, ABI_GUARD, True, "LLP64 guards (__shfl long overloads)"),
    (DEFINES_HEADER, SIZE_T_OLD, SIZE_T_NEW, False, "__hip_internal::size_t on Windows (64-bit)"),
    (HIP_CMAKE, HIP_CMAKE_OPEN_OLD, HIP_CMAKE_OPEN_NEW, False, "compiler-rt only for MSVC (open)"),
    (HIP_CMAKE, HIP_CMAKE_CLOSE_OLD, HIP_CMAKE_CLOSE_NEW, False, "compiler-rt only for MSVC (close)"),
]


def rocm_root(given: str | None) -> Path:
    for c in (given, os.environ.get("ROCM_PATH"), str(DEFAULT_ROCM)):
        if c and (Path(c) / "include" / "hip").is_dir():
            return Path(c)
    sys.exit(f"no ROCm SDK found (looked for {DEFAULT_ROCM}/include/hip); "
             "install it first: python -m pip install --index-url "
             "https://repo.amd.com/rocm/whl-multi-arch/ rocm[libraries,devel,device-gfx1101]==7.14.1")


def patch_headers(root: Path, check: bool) -> str:
    done = []
    for rel, old, new, all_occ, what in PATCHES:
        h = root / rel
        if not h.is_file():
            return f"missing {h}"
        text = h.read_text(encoding="utf-8", errors="surrogateescape")
        if new in text:
            done.append(f"{what}: already patched")
            continue
        n = text.count(old)
        if not n:
            return f"nothing to patch in {rel} (unexpected header layout)"
        if not all_occ and n != 1:
            return f"{rel}: expected 1 site, found {n}"
        if check:
            done.append(f"{what}: would patch {n} site(s)")
            continue
        backup = h.with_suffix(".h.orig")
        if not backup.exists():
            shutil.copy2(h, backup)
        h.write_text(text.replace(old, new), encoding="utf-8", errors="surrogateescape")
        done.append(f"{what}: patched {n} site(s)")
    return "; ".join(done)


def stage_builtins(root: Path, ucrt: Path, check: bool) -> str:
    clang_lib = root / "lib" / "llvm" / "lib" / "clang"
    versions = sorted((p.name for p in clang_lib.glob("*") if p.name.split(".")[0].isdigit()),
                      key=lambda v: [int(x) for x in v.split(".")])
    if not versions:
        return f"no clang version directory under {clang_lib}"
    dst = clang_lib / versions[-1] / "lib" / "x86_64-w64-windows-gnu" / "libclang_rt.builtins.a"
    if dst.is_file():
        return f"already present ({dst.parent})"
    gcc = sorted((ucrt / "lib" / "gcc" / "x86_64-w64-mingw32").glob("*/libgcc.a"))
    if not gcc:
        return f"no libgcc.a under {ucrt}/lib/gcc/x86_64-w64-mingw32"
    if check:
        return f"would copy {gcc[-1].parent.parent.name} libgcc.a -> {dst.parent}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(gcc[-1], dst)
    return f"copied libgcc.a ({gcc[-1].parent.parent.name}) -> {dst.parent}"


def main() -> int:
    ap = argparse.ArgumentParser(description="prepare the ROCm SDK for a MinGW-target clang build")
    ap.add_argument("--rocm", help="ROCm SDK root (default: .venv's _rocm_sdk_devel)")
    ap.add_argument("--ucrt", default=str(DEFAULT_UCRT), help="MSYS2 UCRT64 root")
    ap.add_argument("--check", action="store_true", help="report what would change, change nothing")
    a = ap.parse_args()
    root = rocm_root(a.rocm)
    print(f"ROCm SDK: {root}")
    ok = True
    for what, msg in (("HIP headers (LLP64 guards)", patch_headers(root, a.check)),
                      ("MinGW compiler-rt builtins", stage_builtins(root, Path(a.ucrt), a.check))):
        bad = msg.startswith(("missing", "no ", "nothing"))
        ok &= not bad
        print(f"  [{'X' if bad else 'ok'}] {what}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
