# Maya's HIP engine on Windows without Visual Studio (clang + MinGW/UCRT64)

**Unsupported, experimental, and not a configuration upstream supports.** Maya's Windows HIP
engine is built with Visual Studio Build Tools; this directory builds it with ROCm's clang
targeting the MSYS2 UCRT64 (MinGW) ABI instead, so no administrator install is needed.
`START-MAYA.bat` is unchanged unless `MAYA_HIP_MINGW=1` is set.

## Status on this machine (2026-11-13)

| Step | Result |
|---|---|
| Configure Maya's tree (HIP + hipBLAS + ggml) | ok |
| Compile all 205 targets of `maya` | ok |
| Link `maya.exe` (44 MB) | ok |
| `maya.exe --help` runs | ok |
| GPU parity vs CPU reference on gfx1101 | `hip_intrinsics`, `qsa_parity`, `s_gemv_parity`, `bf16_gemv_parity`, `hip_native_qsa_score`, `hip_prefill_mmq_parity` — **all pass** |
| `hip_prefill_fused_parity` | skipped by design: `src/prefill/moe_fused_rdna4.cu` gates the fused path to gfx1151/gfx1100/gfx1200/gfx1201 |
| `ple_parity` | fails only because it needs a Qwen GGUF that is not downloaded |

A model has **not** been downloaded or run yet (73 GB+).

## The one thing that will waste your afternoon

**HIP device 0 is your integrated GPU.** On a Ryzen with a discrete Radeon, `hipInfo`,
`MIOpenDriver` and every HIP program default to index 0, which is the APU (`gfx1036`) — an
architecture ROCm has no kernels for. The symptom is a *correct-looking* build whose first
kernel launch fails with:

```
device kernel image is invalid
```

while `hipMalloc`/`hipMemcpy` succeed. It is not a bad code object and not an old driver.
Select the discrete card:

```powershell
$env:HIP_VISIBLE_DEVICES = "1"     # from hipInfo's numbering; 1 = the RX 7800 XT here
```

Maya's own GPU picker already skips the APU (its arch is not in the supported list), so the
engine gets the right index from `check_hip_pc()`; only hand-run tests need the variable.

## The eight things that had to be fixed

| # | Problem | Fix |
|---|---|---|
| 1 | `amd_warp_functions.h` guards LP64 `long` shuffles with `#ifndef _MSC_VER`, tripping `static_assert(sizeof(long) == 2*sizeof(int))` | branch on the data model: `#if !defined(_MSC_VER) && !defined(_WIN32)` |
| 2 | `__hip_internal::size_t` is `unsigned long` unless `_MSC_VER` — **32-bit on Windows** — while the global `size_t` is 64-bit; `HIP_vector_type` cannot instantiate | same treatment: `#if defined(_WIN32)` |
| 3 | HIP headers use `__declspec(dllimport)` | `-fdeclspec` |
| 4 | ROCm's clang has no MinGW sysroot and ignores `--gcc-toolchain` | explicit `-isystem` for libstdc++ and UCRT |
| 5 | CMake's HIP ABI detection wants `Platform/Windows-GNU-HIP-ABI.cmake`, which CMake does not ship | `modules/Platform/Windows-GNU-HIP-ABI.cmake` via `-DCMAKE_MODULE_PATH` |
| 6 | `hip-config-amd.cmake` appends clang's **MSVC** compiler-rt to `hip::host`/`hip::device`, needing `__security_check_cookie`/`_fltused` | guarded with `if(MSVC)`; MinGW links libgcc |
| 7 | hipBLASLt's C++ extension API is MSVC-mangled and cannot be linked by a MinGW caller | `-DCMAKE_DISABLE_FIND_PACKAGE_hipblaslt=ON` (gfx1101 has no tuning table anyway) |
| 8 | `find_package(hip CONFIG)` and `-lpthread` | `-DCMAKE_PREFIX_PATH=<rocm>`; `-lwinpthread` on the link line |

Items 1, 2 and 6 are patched in the installed ROCm SDK by `prepare_sdk.py` (originals kept as
`*.orig`). Items 4, 5, 7, 8 live in `configure.ps1`.

The libstdc++ `-isystem` directories must go to **C++ and HIP only**: with them in
`CMAKE_C_FLAGS`, a C file's `<stdatomic.h>` resolves to libstdc++'s C++ wrapper and
`ggml-cpu.c` loses `atomic_int`.

## Use

```powershell
.\.venv\Scripts\python.exe tools\hip\mingw\prepare_sdk.py     # once per ROCm install
.\tools\hip\mingw\configure.ps1 -Build -Tests                 # build-hip/
.\.venv\Scripts\python.exe tools\hip\mingw\stage_runtime.py   # DLLs beside maya.exe
```

Then, for the GPU tests (never without the device selection):

```powershell
$env:HIP_VISIBLE_DEVICES = "1"
$env:PATH = "$PWD\.venv\Lib\site-packages\_rocm_sdk_devel\bin;C:\msys64\ucrt64\bin;$PWD\build-hip;$env:PATH"
.\.venv\Scripts\ctest.exe --test-dir build-hip -R "parity|intrinsics" --output-on-failure
```

Or drive Maya itself: `set MAYA_HIP_MINGW=1` then `START-MAYA.bat --setup`.

## Known portability gaps (audited, test-only — not blocking)

An independent audit recompiled all 135 `src/` C++ TUs and ran a HIP host pass on 103 of
107 `.cu` files on this toolchain: there are **no source-level blockers**, only these:

| Site | Issue | Effect |
|---|---|---|
| `src/kernels/ple_parity.cpp:93-94,108` | `ftell`/`fseek` use 32-bit `long`; the PLE table is 28.8 GB | test-only: reads nothing past 2 GiB and reports nonsense |
| `cpu/pool_test.cpp:40`, `cpu/expert_parity.cpp:51` | `fseeko`/`off_t` is 32-bit under UCRT; `BLOB` = 1,382,400 overflows past index ~1553 | test-only |
| 5 × unguarded `#define NOMINMAX` (`ds4_main.cpp`, `ngram.cpp`, `direct_file.cpp`, `memory.cpp`, `memory_test.cpp`) | libstdc++'s `bits/os_defines.h` already defines it | warning noise; fatal only with `STRATA_WERROR=ON` |
| `cpu/glm_lane_row4_avx2.cpp:50` (`_cvtsh_ss`) | needs `-mf16c` | supplied by `CMakeLists.txt`; would break a hand-written compile loop |
| `exl3_cpu.cpp:44`, `iq_avx512_one.cpp:34` (`__forceinline`) | dead `_MSC_VER` branches | would need `-fms-extensions` if ever live |
| `hip_compat/intrinsics.hpp` vs ggml's `vendors/hip.h` | function-like macros collide | only if the shim is force-included into `strata_mmq`; today's wiring does not |

The correct fix for the offset ones is the idiom the repo already has at
`src/core/pinned.cu:18` (`STRATA_FSEEK64`): key on `_WIN32`, not `_MSC_VER`.

## What it does not fix

- **Speed.** No hipBLASLt tuning table for `gfx1101`, and the fused-expert prefill is gated
  off for it. Correct, slower. Do not widen that list without parity data.
- **Memory.** The models are 73 GB and up; RAM/VRAM limits are unchanged.
- **Upgrades.** Not upstream. A new Maya version needs the flags re-checked, and a ROCm SDK
  reinstall needs `prepare_sdk.py` re-run (both patches are inside `.venv`).
- **C++ ABI boundaries.** Anything in the MSVC-built ROCm stack with a C++ API
  (hipBLASLt extensions, hipDNN, MIOpen's C++ surface) cannot be called from this build.
