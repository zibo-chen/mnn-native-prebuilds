# mnn-native-prebuilds

[![Validate](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/validate.yml/badge.svg)](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/validate.yml)

独立维护 MNN 的跨平台预编译库。仓库保存构建配置、源码版本锁定、兼容性补丁、验证和发布脚本；构建时直接获取 `alibaba/MNN` 的指定 commit，不维护 MNN 源码 Fork。

[下载 Release](https://github.com/zibo-chen/mnn-native-prebuilds/releases) · [手动构建](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/build.yml) · [源码版本](versions/mnn.json) · [构建配置](configs/mnn.json)

## 平台和后端

迁移原 [MNN-Prebuilds](https://github.com/zibo-chen/MNN-Prebuilds) 的 10 个目标，保留 CPU 和 Apple Metal 配置，增加 12 个 GPU / CoreML 组合，共 22 个包。**当前版本、完整 SHA 和补丁以 [版本锁](versions/mnn.json) 为准**。迁移时的 `3.6.0-9070bc73-r1/r2` 使用开发提交，不等同于 3.6.0 正式标签；自动跟进的版本则直接锁定正式 tag 对应的提交。

基础包如下。所有包都包含 CPU 后端，可在应用中显式选择加速后端；具体可用性还取决于设备、驱动和模型算子。

| 目标 | ABI / 运行库 | 后端 | 静态 / 动态 | 基线 |
|---|---|---|---|---|
| linux-x86_64 | GNU / libstdc++ | CPU | 两者 | Ubuntu 24.04 / glibc 2.39 |
| linux-aarch64 | GNU / libstdc++ | CPU | 两者 | Ubuntu 24.04 / glibc 2.39 |
| windows-x86_64 | MSVC / MT | CPU | 两者 | Windows 10 |
| windows-i686 | MSVC / MT | CPU | 两者 | Windows 10 |
| windows-aarch64 | clang-cl，MSVC ABI / MT | CPU | 两者 | Windows 10 ARM64 |
| macos-universal | arm64 + x86_64 / libc++ | CPU、Metal | 两者 | macOS 11.0 |
| ios-arm64 | 真机 / libc++ | CPU、Metal | 静态 | iOS 13.0 |
| ios-arm64-sim | ARM64 模拟器 / libc++ | CPU、Metal | 静态 | iOS 13.0 Simulator |
| android-arm64-v8a | NDK r27c / c++_static | CPU | 两者 | API 21 |
| android-armeabi-v7a | NDK r27c / c++_static | CPU | 两者 | API 21 |

新增组合继承对应基础包的平台、ABI 和静态/动态形式：

| 目标 | 额外后端 |
|---|---|
| linux-x86_64-vulkan-opencl | Vulkan、OpenCL |
| linux-aarch64-vulkan-opencl | Vulkan、OpenCL |
| windows-x86_64-vulkan-opencl | Vulkan、OpenCL |
| windows-i686-vulkan-opencl | Vulkan、OpenCL |
| windows-aarch64-vulkan-opencl | Vulkan、OpenCL |
| android-arm64-v8a-vulkan-opencl-opengl | Vulkan、OpenCL、OpenGL ES |
| android-armeabi-v7a-vulkan-opencl-opengl | Vulkan、OpenCL、OpenGL ES |
| macos-universal-metal-coreml | Metal、CoreML |
| ios-arm64-metal-coreml | Metal、CoreML |
| ios-arm64-sim-metal-coreml | Metal、CoreML |
| linux-x86_64-cuda12 | CUDA 12 |
| windows-x86_64-cuda12 | CUDA 12 |

Linux 包用于 GNU/glibc，不适用于 musl。系统还需要兼容的 C++ 运行库；glibc 基线不代表在所有该版本以上发行版上都已测试。Windows 包使用 Release `/MT`，消费端需匹配 CRT 与 ABI。Android 静态 MNN 消费端需匹配 NDK 和 C++ 运行库；在应用包含多个原生共享库时，应统一规划 C++ 运行库链接。

### 加速后端的运行依赖

- Vulkan / OpenCL 使用 MNN 自带头文件和动态加载机制，构建包无需系统 Vulkan/OpenCL SDK。应用运行仍需对应 loader、GPU 实现和驱动；Linux 支持 `libvulkan.so.1` / `libOpenCL.so.1`。Android 的厂商 OpenCL 库可访问性因设备而异；API 21 是二进制基线，不代表该系统必然具备 Vulkan 或 OpenCL。
- Android OpenGL 后端依赖 OpenGL ES 3.1、EGL 和合适的图形上下文。CMake 静态链接会补齐 `GLESv3` / `EGL`。
- Apple 包使用系统 Metal；CoreML 组合还链接 CoreML / CoreVideo。模型是否使用 GPU 或 Neural Engine 由模型、系统和设备决定。
- CUDA 包固定 **Toolkit 12.8.1**，内含 **SM 75、80、86、89、90、120** 的 cubin，以及 **compute 120 PTX**。覆盖范围需结合实际显卡计算能力判断，未覆盖更早的 GPU。CUTLASS 2.9.0 的源码 SHA 和下载校验也已锁定。
- CUDA/cuBLAS 动态运行库和 NVIDIA 驱动由消费端提供，不随 MNN 包分发。使用 CMake 包链接时需要 **CUDA Toolkit >=12.8,<13** 的头文件和库，无需重新编译 MNN 的 CUDA 内核。运行时将对应 CUDA `bin`（Windows）或 `lib64`（Linux）放到库搜索路径；驱动必须支持实际 GPU 和所用 cubin/PTX。

**Linux CUDA 静态 MNN 仍有动态依赖**：`libMNN.a` 配套 `lib/cuda-static/libMNN_Cuda_Main.so`；动态 MNN 配套 `lib/libMNN_Cuda_Main.so`。两种链接形式的 companion 分别构建，CMake 会选择正确的文件。部署静态消费程序时也要部署对应的 companion，并保持该目录可被加载。Windows 的 CUDA 对象已合入 `MNN_static.lib` 或 `MNN.dll`，但仍依赖 CUDA/cuBLAS DLL。

## 使用预编译包

下载后先验证 `SHA256SUMS`，再解压对应的 `.tar.gz` 或 `.zip`。每包包含：

```text
mnn-<package-version>-<target>/
├── include/MNN/             # Core 和 Express 头文件
├── lib/                    # 静态库、动态库或 Windows 导入库
│   └── cmake/MNN/          # 可重定位的 find_package 配置
├── manifest.json           # 源码 SHA、构建 SHA、参数、验证结果、文件哈希
├── metadata/               # 源码锁和兼容性补丁
└── licenses/               # 第三方许可证
```

Windows 静态库命名为 `MNN_static.lib`，动态库为 `MNN.dll` 和 `MNN.lib`；其他平台使用 `libMNN.a`、`libMNN.so` 或 `libMNN.dylib`。

使用 CMake 3.24+：

```cmake
find_package(MNN CONFIG REQUIRED)
target_link_libraries(your_app PRIVATE MNN::MNN)
```

可要求特定后端，选错包时在配置阶段报错：

```cmake
find_package(MNN CONFIG REQUIRED COMPONENTS vulkan opencl)
message(STATUS "Packaged backends: ${MNN_AVAILABLE_BACKENDS}")
target_link_libraries(your_app PRIVATE MNN::MNN)
```

组件名为 `cpu`、`metal`、`coreml`、`vulkan`、`opencl`、`opengl`、`cuda`。`find_package` 只校验包内功能，不会为应用选择推理设备。应用仍应设置 `MNN::ScheduleConfig::type`，例如 `MNN_FORWARD_VULKAN`、`MNN_FORWARD_OPENCL`、`MNN_FORWARD_CUDA`；Apple CoreML 使用 `MNN_FORWARD_NN`。需要关注 MNN 的算子回退和设备不可用时的行为。

配置时传 `-DCMAKE_PREFIX_PATH=/path/to/extracted-package`。默认静态链接，并保留 MNN 算子注册对象。动态链接传 `-DMNN_USE_STATIC_LIBS=OFF`，部署时将动态库放到系统可搜索的位置。Windows 动态链接配置会自动添加 `USING_MNN_DLL`。

`ocr-rs` 可先使用现有外部库接口接入，库和头文件必须来自同一个包：

```sh
MNN_LIB_DIR=/path/to/package/lib \
MNN_INCLUDE_DIR=/path/to/package/include \
cargo build --features mnn-static
```

Apple Metal 功能按消费端需要另行启用。旧版 `ocr-rs` 的自动下载仍指向旧仓库；此项目不会更改旧 Release，也不会自动改写下游项目。

## 构建和发布

### 自动跟进上游 tag

[Check upstream MNN tags](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/upstream.yml) 每 6 小时检查一次 `alibaba/MNN` 的 Git tags（UTC 的 00:23、06:23、12:23、18:23）。上游只推送 tag、没有创建 GitHub Release 页面，也会被发现。

- 识别 `3.6.1`、`v3.6.1` 这类正式版本，按数值版本排序；暂不跟进 `rc`、`beta`、Android 专用标签等。首次启用会补构建比当前版本锁更新的正式版本。一次处理一个版本，多个新版本按顺序处理。
- 将附注 tag 解析到完整 commit SHA，先验证源码版本宏和全部兼容补丁，再由 `github-actions[bot]` 提交 `versions/mnn.json`。新版本从打包修订 `r1` 开始，保留已有后端、依赖锁和补丁配置。
- 显式触发完整矩阵的构建与发布，将版本锁所在的 **builder commit SHA** 传给所有构建/发布 job，避免构建期间 `main` 更新导致包混用不同配方。只需要仓库自带的 `GITHUB_TOKEN`，无需配置 PAT 或上游 webhook。
- 已发布的版本直接跳过；同一构建提交正在运行或已经尝试过时，不会重复启动。若提交版本锁后 dispatch 中断，下次检查会恢复启动。
- 构建失败不发布、不覆盖旧包；可以在失败的构建上选择 **Re-run failed jobs**。修复配方并推送新的 builder commit 后，下次检查会重试尚未发布的当前版本。后续更高版本 tag 仍可被发现。tag 被移动/删除、存在同名草稿 Release 或补丁不兼容时会明确失败，等待维护者处理。

立即检查并按需构建，或者只预览发现结果：

```sh
gh workflow run upstream.yml --repo zibo-chen/mnn-native-prebuilds
gh workflow run upstream.yml --repo zibo-chen/mnn-native-prebuilds -F dry_run=true

# 本地只读预览，不修改仓库、不启动构建
python3 scripts/check_upstream.py
```

检查结果显示在对应 Actions run 的 Summary 中。该流程会自动向默认分支提交版本锁，因此若以后启用分支保护，需要允许机器人更新该文件或调整更新策略。GitHub 定时任务可能延迟；公开仓库连续 60 天没有活动时，定时工作流会被自动停用，需要在 Actions 页面重新启用，详见 [GitHub 的说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。

### 手动构建

GitHub Actions 的 `Build MNN packages` 支持 `target=all`、`target=backends`（仅 12 个扩展组合）或配置文件中的单个目标。源码完全由当前构建仓库 commit 中的 `versions/mnn.json` 决定；选择哪个构建分支，就是使用哪个分支的版本锁和配方。

```sh
# 构建全部目标，验证后保存在 Actions artifacts
gh workflow run build.yml --repo zibo-chen/mnn-native-prebuilds -f target=all -F publish=false

# 全部目标验证通过后发布新的 Release
gh workflow run build.yml --repo zibo-chen/mnn-native-prebuilds -f target=all -F publish=true
```

只有完整矩阵可以发布。发布前重新验证压缩包内容、SHA-256、源码/补丁/构建 commit 一致性，以及消费端测试记录。Release 附带 `index.json`、`SHA256SUMS` 和各包独立清单。已有 Release 不会被覆盖；更新打包方式时应增加 `revision`，同步更新 `package_version` 和 `release_tag`。

工作流还接受可选的 `builder_ref` 完整 SHA；自动检查器使用此参数固定配方版本，手动构建通常留空即可。工作流并发组也按 builder SHA 和目标隔离，同一提交的相同目标不会同时构建。

本地构建使用 Python 3.9+、Git、Ninja 和目标工具链，推荐与 CI 一致的 CMake 3.31.6。CUDA 配方使用上游的 FindCUDA，暂不支持用 CMake 4 构建。CI 固定 CMake 3.31.6、Ninja 1.11.1.3、Android NDK 27.2.12479018、CUDA Toolkit 12.8.1；系统编译器随固定操作系统系列的 runner image 更新，实际版本记录在 manifest 中，因此目前不承诺位级一致的构建结果。

```sh
python3 -m pip install cmake==3.31.6 ninja==1.11.1.3
python3 -m unittest discover -s tests -v
python3 recipes/mnn/build.py plan --target all
python3 recipes/mnn/build.py prepare
python3 recipes/mnn/build.py build --target macos-universal
```

`prepare` 在 `.work/source` 拉取固定上游 SHA，检查并应用补丁。它拒绝来源不符或存在额外修改的源码，不会重置用户的源码目录。KleidiAI / CUTLASS 的下载 SHA-256 已锁定，下载失败不会静默降级。修改锁文件后应使用新的源码目录；重复构建时使用新的 `--work` 路径。本地 CUDA 构建需先安装固定 Toolkit，将 `nvcc` / `cuobjdump` 加入 PATH，设置 `CUDA_PATH`；默认以 2 个并行任务限制内存占用。

## 验证范围

- 所有目标：编译 MNN，并从打包目录使用 `find_package(MNN)` 编译、链接一个独立消费程序；静态和动态库分别验证。
- 原生桌面架构：运行 CPU 表达式图，检查输入经过乘加计算后输出为 `3, 5, 7, 9`。
- Linux x86_64 Vulkan 组合：使用 Mesa Lavapipe 软件设备执行相同表达式，并拒绝将请求的后端不可用、回退 CPU 的情况计为通过。这是 Vulkan API 路径验证，不是物理 GPU 性能测试。OpenCL 的 MNN 实现要求 GPU 设备，不能直接用 PoCL CPU 设备替代验证。
- CUDA：用 `cuobjdump` 检查最终库中的 cubin / PTX 架构集合；验证两种链接形式的 companion 和 CUDA/cuBLAS 依赖。
- macOS Universal：检查每个库同时包含 arm64 和 x86_64；分别链接消费程序，在匹配 runner 的架构上执行。
- Windows：额外检查静态库没有不兼容的 STL vector helper 引用和编译器 PDB 引用。
- 压缩包：检查完整文件集合、内嵌清单与外部清单一致、逐文件 SHA-256。

Linux ARM64、Windows ARM64、Android 和 iOS 在当前矩阵中仅进行交叉编译和消费端链接，尚未执行目标设备推理。CUDA、OpenCL、OpenGL、Metal、CoreML 的硬件推理测试需在有可用设备的 runner 上另行完成。清单逐后端记录验证状态；构建、链接通过不等同于硬件推理验证通过。

## 扩展配置

平台参数放在 `configs/mnn.json` 的 `targets`，后端参数放在 `profiles`，组合放在 `variants`。每个组合通过 `base` 继承基础目标，按需覆盖 profile、CMake 参数和依赖；不需要复制整套平台配置。新增后端时还需补上 SDK 安装、依赖打包、CMake 导出和验证。

完整 OpenCV 应新增独立 `recipes/opencv` 配方和版本锁；MNN 自带的 CV 则是 MNN 配置中的功能。每个组件保持独立版本和产物，避免与所有 GPU 配置形成重复的组合。

## 许可证

构建脚本和迁移的兼容性补丁使用 Apache-2.0。MNN 和其依赖遵循各自许可证，二进制包附带对应声明。补丁来源和用途见 [patches/mnn/README.md](patches/mnn/README.md)。
