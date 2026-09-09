# MNN compatibility patches

These patches preserve the build fixes from
[zibo-chen/MNN-Prebuilds](https://github.com/zibo-chen/MNN-Prebuilds).
They were introduced for upstream commit `9070bc73ae40500bc7202975142b43f488bf10d8`
and also apply to the official `3.6.1` tag (`d407447ed56c4121a11ccbd266dc184ca1ead0c2`).
The automated tag checker verifies applicability before committing a new source lock.

- `0001`: Raise the root CMake minimum to 3.10; avoid GNU ARM assembly with
  native MSVC; avoid KleidiAI assembly on Windows. Migrated from the old fork's
  diff against its upstream merge parent. Windows ARM64 continues using clang-cl.
- `0002`: Remove `/Zi` from Release compiler flags so distributed static libraries
  do not reference unavailable compiler PDB files. Equivalent to the source fix
  in old-fork commit `419ec534e874d8d76877ddbe035eddb8001373c2`.
- `0003`: Honor an explicit, multi-architecture `CUDA_ARCHS` list instead of
  upstream's broad default gencode list. Keep architecture-specific definitions
  separate, and honor the checksum-verified CUTLASS FetchContent source override.
- `0004`: Try Linux's versioned Vulkan/OpenCL loader sonames before the existing
  search paths, so development-package symlinks are not required at runtime.

`_USE_STD_VECTOR_ALGORITHMS=0` is a compiler definition in the build recipe,
not a source patch. Windows packages are checked for both vector-helper
references and compiler PDB references before publishing.

The source preparation step checks patch applicability and records patch SHA-256
hashes. A version upgrade must review each patch against the new upstream source.
Do not silently ignore failed patches or apply these patches to an arbitrary ref.
