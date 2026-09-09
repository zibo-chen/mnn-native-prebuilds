# mnn-native-prebuilds

[![Validate](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/validate.yml/badge.svg)](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/validate.yml)

独立维护 MNN 的跨平台预编译库。仓库保存构建配置、源码版本锁定、兼容性补丁、验证和发布脚本；构建时直接获取 `alibaba/MNN` 的指定 commit，不维护 MNN 源码 Fork。

[下载 Release](https://github.com/zibo-chen/mnn-native-prebuilds/releases) · [手动构建](https://github.com/zibo-chen/mnn-native-prebuilds/actions/workflows/build.yml) · [源码版本](versions/mnn.json) · [构建配置](configs/mnn.json)

## 首版范围

迁移原 [MNN-Prebuilds](https://github.com/zibo-chen/MNN-Prebuilds) 的 10 个目标，保留 CPU 和 Apple Metal 配置。首版固定到原仓库最近一次成功构建所用的上游提交 `9070bc73`，MNN 版本宏为 `3.6.0`。**这个提交晚于 3.6.0 标签，不等同于该标签的源码。** 完整 SHA 和补丁见版本锁文件。

CUDA、Vulkan、OpenCL、MNN CV、完整 OpenCV 和新增平台作为后续扩展；首版没有发布这些功能的包。Metal 表示后端已编译进库，不表示 CI 已验证 GPU 推理。

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

Linux 包用于 GNU/glibc，不适用于 musl。系统还需要兼容的 C++ 运行库；glibc 基线不代表在所有该版本以上发行版上都已测试。Windows 包使用 Release `/MT`，消费端需匹配 CRT 与 ABI。Android 静态 MNN 消费端需匹配 NDK 和 C++ 运行库；在应用包含多个原生共享库时，应统一规划 C++ 运行库链接。

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

配置时传 `-DCMAKE_PREFIX_PATH=/path/to/extracted-package`。默认静态链接，并保留 MNN 算子注册对象。动态链接传 `-DMNN_USE_STATIC_LIBS=OFF`，部署时将动态库放到系统可搜索的位置。Windows 动态链接配置会自动添加 `USING_MNN_DLL`。

`ocr-rs` 可先使用现有外部库接口接入，库和头文件必须来自同一个包：

```sh
MNN_LIB_DIR=/path/to/package/lib \
MNN_INCLUDE_DIR=/path/to/package/include \
cargo build --features mnn-static
```

Apple Metal 功能按消费端需要另行启用。旧版 `ocr-rs` 的自动下载仍指向旧仓库；此项目不会更改旧 Release，也不会自动改写下游项目。

## 构建和发布

GitHub Actions 的 `Build MNN packages` 支持 `target=all` 或配置文件中的单个目标。源码完全由当前构建仓库 commit 中的 `versions/mnn.json` 决定；选择哪个构建分支，就是使用哪个分支的版本锁和配方。

```sh
# 构建全部目标，验证后保存在 Actions artifacts
gh workflow run build.yml --repo zibo-chen/mnn-native-prebuilds -f target=all -F publish=false

# 全部目标验证通过后发布新的 Release
gh workflow run build.yml --repo zibo-chen/mnn-native-prebuilds -f target=all -F publish=true
```

只有完整矩阵可以发布。发布前重新验证压缩包内容、SHA-256、源码/补丁/构建 commit 一致性，以及消费端测试记录。Release 附带 `index.json`、`SHA256SUMS` 和各包独立清单。已有 Release 不会被覆盖；更新打包方式时应增加 `revision`，同步更新 `package_version` 和 `release_tag`。

本地构建使用 Python 3.9+、Git、CMake 3.24+、Ninja 和目标工具链。CI 固定 CMake 3.31.6、Ninja 1.11.1.3、Android NDK 27.2.12479018；系统编译器随固定操作系统系列的 runner image 更新，实际版本记录在 manifest 中，因此目前不承诺位级一致的构建结果。

```sh
python3 -m pip install cmake==3.31.6 ninja==1.11.1.3
python3 -m unittest discover -s tests -v
python3 recipes/mnn/build.py plan --target all
python3 recipes/mnn/build.py prepare
python3 recipes/mnn/build.py build --target macos-universal
```

`prepare` 在 `.work/source` 拉取固定上游 SHA，检查并应用补丁。它拒绝来源不符或存在额外修改的源码，不会重置用户的源码目录。ARM64 所用 KleidiAI 版本和下载 SHA-256 也已锁定，下载失败不会静默降级。修改锁文件后应使用新的源码目录；重复构建时使用新的 `--work` 路径。

## 验证范围

- 所有目标：编译 MNN，并从打包目录使用 `find_package(MNN)` 编译、链接一个独立消费程序；静态和动态库分别验证。
- 原生桌面架构：运行 CPU 表达式图，检查输入经过乘加计算后输出为 `3, 5, 7, 9`。
- macOS Universal：检查每个库同时包含 arm64 和 x86_64；分别链接消费程序，在匹配 runner 的架构上执行。
- Windows：额外检查静态库没有不兼容的 STL vector helper 引用和编译器 PDB 引用。
- 压缩包：检查完整文件集合、内嵌清单与外部清单一致、逐文件 SHA-256。

Linux ARM64、Windows ARM64、Android 和 iOS 在当前矩阵中仅进行交叉编译和消费端链接，尚未执行目标设备推理。Metal GPU 测试也需在有可用设备的 runner 上另行完成。构建、链接通过不等同于硬件推理验证通过。

## 扩展配置

平台参数放在 `configs/mnn.json` 的 `targets`，后端参数放在 `profiles`。新增后端时还需补上 SDK 安装、完整依赖打包、CMake 导出和测试；仅增加一个 CMake 开关不能视为完成支持。CUDA 尤其需要处理 Linux `libMNN_Cuda_Main.so`、CUDA/cuBLAS 依赖和 GPU 架构列表。

完整 OpenCV 应新增独立 `recipes/opencv` 配方和版本锁；MNN 自带的 CV 则是 MNN 配置中的功能。每个组件保持独立版本和产物，避免与所有 GPU 配置形成重复的组合。

## 许可证

构建脚本和迁移的兼容性补丁使用 Apache-2.0。MNN 和其依赖遵循各自许可证，二进制包附带对应声明。补丁来源和用途见 [patches/mnn/README.md](patches/mnn/README.md)。
