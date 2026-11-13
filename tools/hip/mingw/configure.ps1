# Configure (and optionally build) Maya's HIP engine on Windows with clang + the MinGW/UCRT64
# ABI, instead of Visual Studio Build Tools.
#
#   .\tools\hip\mingw\configure.ps1                 configure only
#   .\tools\hip\mingw\configure.ps1 -Build          configure, then build the `maya` target
#   .\tools\hip\mingw\configure.ps1 -Build -Tests   also build the test targets (parity checks)
#
# Unsupported experiment; see README.md next to this file. The MSVC path (`START-MAYA.bat`)
# is untouched by anything here.

param(
    [string]$BuildDir   = "build-hip",
    [string]$GgmlDir    = "",
    [string]$Arch       = "gfx1101",
    [int]$Jobs          = 4,
    [switch]$Build,
    [switch]$Tests
)

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$Venv = Join-Path $Repo ".venv"
$Rocm = Join-Path $Venv "Lib\site-packages\_rocm_sdk_devel"
$Bc   = Join-Path $Rocm "lib\llvm\amdgcn\bitcode"
$Ucrt = "C:/msys64/ucrt64"

function Fwd([string]$p) { $p.Replace('\', '/') }

if (-not (Test-Path (Join-Path $Rocm "lib\llvm\bin\clang++.exe"))) {
    throw "no ROCm SDK at $Rocm - run: .\.venv\Scripts\python.exe tools\hip\mingw\prepare_sdk.py"
}
if (-not (Test-Path "C:\msys64\ucrt64\bin")) { throw "no MSYS2 UCRT64 at C:\msys64\ucrt64" }

# MinGW's libgcc directory carries the gcc version in its path (16.2.0 today)
$GccDir = Get-ChildItem (Join-Path $Ucrt "lib\gcc\x86_64-w64-mingw32") -Directory -ErrorAction SilentlyContinue |
          Sort-Object Name | Select-Object -Last 1
if (-not $GccDir) { throw "no libgcc under $Ucrt/lib/gcc/x86_64-w64-mingw32" }
$GccLib = Fwd $GccDir.FullName
$CxxInc = Fwd (Join-Path $Ucrt "include/c++/$($GccDir.Name)")
$Target = Fwd (Join-Path $Ucrt "include/c++/$($GccDir.Name)/x86_64-w64-mingw32")

if (-not $GgmlDir) {
    # setup.py's own location first (get_llama_cpp), then any hand-made checkout
    $Candidates = @(Join-Path $Repo "third_party\llama.cpp") + @(
        Get-ChildItem (Join-Path $Repo "third_party") -Directory -Filter "llama.cpp-*" -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName })
    $GgmlDir = $Candidates | Where-Object { Test-Path (Join-Path $_ "ggml\CMakeLists.txt") } |
               Select-Object -First 1
    if (-not $GgmlDir) {
        throw "no llama.cpp source under third_party/ - pass -GgmlDir <dir> (must contain ggml/CMakeLists.txt)"
    }
}

# --target and the include/library search paths are flags, not a toolchain file: ROCm's clang
# ships no MinGW sysroot, and --gcc-toolchain is silently ignored for this target.
#
# The libstdc++ include directories go to C++ and HIP ONLY: with them in CMAKE_C_FLAGS, a C
# file's <stdatomic.h> resolves to libstdc++'s C++ wrapper (include/c++/<v>/stdatomic.h),
# which defines nothing in C, and ggml-cpu.c then has no atomic_int.
$Common = @("--target=x86_64-w64-windows-gnu", "-fdeclspec") -join " "
$Libs = @("-L $Ucrt/lib", "-L $GccLib") -join " "
$FlagsC = "$Common -isystem $(Fwd $Rocm)/include -isystem $Ucrt/include $Libs"
$FlagsXX = "$Common -isystem $(Fwd $Rocm)/include -isystem $CxxInc -isystem $Target -isystem $Ucrt/include $Libs"

$HipFlags = "$FlagsXX --rocm-path=$(Fwd $Rocm) --rocm-device-lib-path=$(Fwd $Bc)"

$Clang = Fwd (Join-Path $Rocm "lib\llvm\bin\clang.exe")
$ClangXX = Fwd (Join-Path $Rocm "lib\llvm\bin\clang++.exe")
$Ninja = Fwd (Join-Path $Venv "Scripts\ninja.exe")
$Cmake = Join-Path $Venv "Scripts\cmake.exe"

$Cfg = @(
    "-S", (Fwd $Repo), "-B", (Fwd (Join-Path $Repo $BuildDir)), "-G", "Ninja",
    "-DCMAKE_MODULE_PATH=$(Fwd $Repo)/tools/hip/mingw/modules",
    "-DCMAKE_MAKE_PROGRAM=$Ninja",
    "-DCMAKE_C_COMPILER=$Clang",
    "-DCMAKE_CXX_COMPILER=$ClangXX",
    "-DCMAKE_HIP_COMPILER=$ClangXX",
    "-DCMAKE_LINKER=lld",
    "-DCMAKE_PREFIX_PATH=$(Fwd $Rocm)",
    "-DCMAKE_HIP_COMPILER_ROCM_ROOT=$(Fwd $Rocm)",
    "-DCMAKE_HIP_ARCHITECTURES=$Arch",
    "-DSTRATA_ENABLE_HIP=ON",
    "-DSTRATA_ENABLE_CUDA=OFF",
    "-DSTRATA_PREFILL_MMQ=ON",
    "-DSTRATA_NATIVE_EXPERTS=ON",
    "-DSTRATA_PORTABLE=ON",
    "-DSTRATA_BUILD_TESTS=$(if ($Tests) { 'ON' } else { 'OFF' })",
    "-DSTRATA_GGML_DIR=$(Fwd $GgmlDir)",
    # hipBLASLt's C++ extension API (hipblaslt_ext::getAlgosFromIndex, ...) is exported from
    # AMD's MSVC-built .lib with MSVC name mangling; a MinGW C++ caller cannot link it.  The
    # C API would be fine, but Maya's solution-table dispatch uses the C++ part, and gfx1101
    # has no tuning table anyway: plain hipBLAS is the fallback this arch takes regardless.
    "-DCMAKE_DISABLE_FIND_PACKAGE_hipblaslt=ON",
    "-DCMAKE_C_FLAGS=$FlagsC",
    "-DCMAKE_CXX_FLAGS=$FlagsXX",
    "-DCMAKE_HIP_FLAGS=$HipFlags",
    # the #pragma comment(lib) sites in src/ds4/ds4_main.cpp and src/mm3/mm3_main.cpp are
    # MSVC-only; MinGW ignores them, so the libraries go on the link line instead.
    # -lwinpthread: MinGW's pthread is libwinpthread, and targets that link -lpthread would
    # otherwise resolve the header but not the symbols.
    # -rtlib=libgcc matters: without it clang picks its MSVC-flavoured compiler-rt for a
    # Windows target (clang_rt.builtins-x86_64.lib), which needs the MSVC CRT symbols
    # (__security_check_cookie, _fltused) that a MinGW link does not have.
    "-DCMAKE_EXE_LINKER_FLAGS=-lwinmm -lpsapi -lwinpthread -rtlib=libgcc"
)

Write-Host "ROCm:   $Rocm"
Write-Host "llama:  $GgmlDir"
Write-Host "arch:   $Arch"
Write-Host "C:      $FlagsC"
Write-Host "C++:    $FlagsXX"
Write-Host ""

& $Cmake @Cfg
if ($LASTEXITCODE -ne 0) { throw "configure failed ($LASTEXITCODE)" }

if ($Build) {
    $Targets = if ($Tests) { @() } else { @("--target", "maya") }
    & $Cmake --build (Fwd (Join-Path $Repo $BuildDir)) @Targets -j $Jobs
    if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }
    Write-Host ""
    Write-Host "built; stage the runtime DLLs next:"
    Write-Host "  .\.venv\Scripts\python.exe tools\hip\mingw\stage_runtime.py"
}
