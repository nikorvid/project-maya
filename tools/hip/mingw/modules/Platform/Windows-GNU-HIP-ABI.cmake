# CMake ships Windows-Clang-HIP.cmake (which handles the GNU frontend variant via
# __windows_compiler_clang_gnu), but no Windows-GNU-HIP-ABI.cmake: detecting the ABI of a
# MinGW-target clang for the HIP language stops with "include could not find requested file".
# This is the missing file, reached through -DCMAKE_MODULE_PATH=tools/hip/mingw/modules.
#
# Unsupported by upstream CMake and by Maya: see tools/hip/mingw/README.md.
include(Platform/Windows-GNU)
__windows_compiler_gnu_abi(HIP)
