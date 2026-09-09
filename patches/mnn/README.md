# MNN compatibility patches

These patches preserve the build fixes from
[zibo-chen/MNN-Prebuilds](https://github.com/zibo-chen/MNN-Prebuilds).
They apply to upstream commit `9070bc73ae40500bc7202975142b43f488bf10d8`.

- `0001`: Raise the root CMake minimum to 3.10; avoid GNU ARM assembly with
  native MSVC; avoid KleidiAI assembly on Windows. Migrated from the old fork's
  diff against its upstream merge parent. Windows ARM64 continues using clang-cl.
- `0002`: Remove `/Zi` from Release compiler flags so distributed static libraries
  do not reference unavailable compiler PDB files. Equivalent to the source fix
  in old-fork commit `419ec534e874d8d76877ddbe035eddb8001373c2`.

`_USE_STD_VECTOR_ALGORITHMS=0` is a compiler definition in the build recipe,
not a source patch. Windows packages are checked for both vector-helper
references and compiler PDB references before publishing.

The source preparation step checks patch applicability and records patch SHA-256
hashes. A version upgrade must review each patch against the new upstream source.
Do not silently ignore failed patches or apply these patches to an arbitrary ref.
