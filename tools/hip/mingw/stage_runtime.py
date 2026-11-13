#!/usr/bin/env python3
"""Stage the runtime DLLs a MinGW-target HIP build of Maya needs beside maya.exe.

Maya's normal Windows build (MSVC) gets its C++ runtime from Windows.  A build with
MAYA_HIP_MINGW=1 links against MSYS2's UCRT64 runtime instead, and ROCm's clang ships no
copy of it: libstdc++-6.dll, libgcc_s_seh-1.dll and libwinpthread-1.dll must sit next to
maya.exe, as must the ROCm runtime the build linked (as copy_hip_dlls does for the MSVC
build too).  Windows searches the program's own folder before System32 and PATH, where a
driver's older amdhip64 can win.

Idempotent: a DLL already there with the same size and timestamp is left alone.  Exits
nonzero when a required DLL is missing or cannot be copied.

    python tools/hip/mingw/stage_runtime.py                      (build-hip, from .venv's ROCm)
    python tools/hip/mingw/stage_runtime.py --dest build         (another build folder)
    python tools/hip/mingw/stage_runtime.py --check              report only, change nothing
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
DEFAULT_DEST = ROOT / "build-hip"
# what ROCm's clang needs from MSYS2 (the three are required: the engine links them)
MINGW_DLLS = ("libstdc++-6.dll", "libgcc_s_seh-1.dll", "libwinpthread-1.dll")
# the ROCm runtime the engine was built with: amdhip64 required, the rest when present.
# hipblas.dll + rocblas.dll are imported directly by maya.exe (hipblas is Maya's GEMM path
# when hipBLASLt is unavailable, which a MinGW build always is: see configure.ps1).
ROCM_DLLS = ("amdhip64*.dll", "amd_comgr*.dll", "rocm_kpack*.dll",
             "hipblas.dll", "rocblas.dll", "rocm-openblas*.dll")
REQUIRED_ROCM = ("amdhip64*.dll", "hipblas.dll", "rocblas.dll")


def rocm_root(given: str | None) -> Path:
    for c in (given, os.environ.get("ROCM_PATH"), str(DEFAULT_ROCM)):
        if c and (Path(c) / "bin").is_dir():
            return Path(c)
    sys.exit(f"no ROCm SDK with a bin folder found (looked for {DEFAULT_ROCM}/bin); "
             "install it first or pass --rocm <root>")


def stage(src: Path, dest: Path, check: bool) -> str:
    """Copy src into dest unless the same file is already there (size and timestamp)."""
    dst = dest / src.name
    size = src.stat().st_size
    if dst.is_file() and dst.stat().st_size == size and dst.stat().st_mtime_ns == src.stat().st_mtime_ns:
        return "already staged"
    if check:
        return f"would stage ({size:,} bytes)"
    shutil.copy2(src, dst)
    return f"staged ({size:,} bytes)"


def main() -> int:
    ap = argparse.ArgumentParser(description="stage the MinGW/ROCm runtime DLLs beside maya.exe")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="the build folder (maya.exe's), default build-hip")
    ap.add_argument("--rocm", help="ROCm SDK root (default: .venv's _rocm_sdk_devel, else ROCM_PATH)")
    ap.add_argument("--ucrt", default=str(DEFAULT_UCRT), help="MSYS2 UCRT64 root")
    ap.add_argument("--check", action="store_true", help="report what would be staged, change nothing")
    a = ap.parse_args()

    if os.name != "nt":
        sys.exit("this script stages Windows DLLs (run it on Windows)")

    dest, ucrt = Path(a.dest), Path(a.ucrt)
    if not dest.is_dir():
        sys.exit(f"no build folder at {dest} (build the engine first, or pass --dest <folder>)")
    rocm = rocm_root(a.rocm)
    ucrt_bin, rocm_bin = ucrt / "bin", rocm / "bin"
    print(f"ROCm SDK:     {rocm}")
    print(f"MSYS2 UCRT64: {ucrt}")
    print(f"beside:       {dest}")

    failed = []
    for pattern in ROCM_DLLS:
        found = sorted(rocm_bin.glob(pattern))
        if not found and pattern in REQUIRED_ROCM:
            failed.append(f"no {pattern} in {rocm_bin}")
            print(f"  [X] {pattern}: no match in {rocm_bin}")
            continue
        for src in found:
            try:
                print(f"  [ok] {src.name}: {stage(src, dest, a.check)}")
            except OSError as e:
                failed.append(f"{src.name}: {e}")
                print(f"  [X] {src.name}: could not copy ({e})")

    for name in MINGW_DLLS:
        src = ucrt_bin / name
        if not src.is_file():
            failed.append(f"{name} is not in {ucrt_bin}")
            print(f"  [X] {name}: not in {ucrt_bin} (pacman -S mingw-w64-ucrt-x86_64-gcc)")
            continue
        try:
            print(f"  [ok] {name}: {stage(src, dest, a.check)}")
        except OSError as e:
            failed.append(f"{name}: {e}")
            print(f"  [X] {name}: could not copy ({e})")

    if failed:
        print(f"{len(failed)} required DLL(s) missing or not staged:")
        for why in failed:
            print(f"  - {why}")
        return 1
    print("runtime complete" + (" (nothing copied: --check)" if a.check else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
