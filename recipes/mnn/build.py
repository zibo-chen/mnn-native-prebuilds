#!/usr/bin/env python3
"""Pinned MNN source preparation, cross-platform builds and package verification."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BACKEND_OPTIONS = {name: "MNN_" + name.upper() for name in
                   ("metal", "cuda", "vulkan", "opencl", "opengl", "coreml")}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stream_digest(handle):
    checksum = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        checksum.update(chunk)
    return checksum.hexdigest()


def file_digest(path):
    with Path(path).open("rb") as handle:
        return stream_digest(handle)


def backend_names(config, target):
    options = {**config["common"], **config["profiles"][target["profile"]], **target["cmake"]}
    return ["cpu"] + [name for name, option in BACKEND_OPTIONS.items() if options[option] == "ON"]


def run(args, cwd=None, capture=False, env=None):
    if not capture:
        print("+ " + " ".join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    return result.stdout.decode("utf-8", errors="replace").strip() if capture else None


def configuration():
    lock = read_json(ROOT / "versions/mnn.json")
    config = read_json(ROOT / "configs/mnn.json")
    if not re.fullmatch(r"[0-9a-f]{40}", lock["ref"]):
        raise ValueError("The upstream ref must be a full commit SHA")
    for key in ("package_version", "release_tag"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", lock[key]):
            raise ValueError("Invalid " + key)
    if lock["repository"] != "alibaba/MNN":
        raise ValueError("Source repository must be alibaba/MNN")
    baselines = copy.deepcopy(config["targets"])
    for name, variant in config.pop("variants", {}).items():
        if name in baselines or variant["base"] not in baselines:
            raise ValueError("Variant must inherit a distinct baseline target: " + name)
        base = copy.deepcopy(baselines[variant["base"]])
        config["targets"][name] = {**base, **variant, "cmake": {**base["cmake"], **variant.get("cmake", {})}}
    for name, target in config["targets"].items():
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Invalid target name")
        if target["profile"] not in config["profiles"]:
            raise ValueError("Unknown profile for " + name)
        backends = backend_names(config, target)
        if {"metal", "coreml"}.intersection(backends) and target["os"] not in ("macos", "ios"):
            raise ValueError("Metal and CoreML require an Apple target")
        if "opengl" in backends and target["os"] != "android":
            raise ValueError("OpenGL packages require Android")
        if "cuda" in backends:
            if target["os"] not in ("linux", "windows") or target["archs"] != ["x86_64"]:
                raise ValueError("CUDA packages require Linux or Windows x86_64")
            if not target.get("cuda", {}).get("architectures"):
                raise ValueError("CUDA architectures must be explicit")
        if not set(target.get("software_backend_tests", [])).issubset(backends):
            raise ValueError("Cannot test an unavailable backend")
    return lock, config


def matrix(selection):
    lock, config = configuration()
    if selection == "all":
        names = list(config["targets"])
    elif selection == "backends":
        names = [name for name, target in config["targets"].items() if "base" in target]
    else:
        names = [selection]
    if any(name not in config["targets"] for name in names):
        raise ValueError("Unknown target: " + selection)
    return lock, {"include": [dict(target=name, **config["targets"][name]) for name in names]}


def patch_records(lock):
    return [{"path": name, "sha256": digest((ROOT / name).read_bytes())}
            for name in lock["patches"]]


def prepare(source, lock=None):
    if lock is None:
        lock, _ = configuration()
    source = source.resolve()
    if not source.exists():
        source.mkdir(parents=True)
        run(["git", "init", source])
        run(["git", "config", "core.autocrlf", "false"], cwd=source)
        run(["git", "config", "core.longpaths", "true"], cwd=source)
        run(["git", "remote", "add", "origin", "https://github.com/" + lock["repository"] + ".git"], cwd=source)
        run(["git", "fetch", "--depth=1", "origin", lock["ref"]], cwd=source)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source)
    if run(["git", "rev-parse", "HEAD"], cwd=source, capture=True) != lock["ref"]:
        raise ValueError("Source HEAD does not match versions/mnn.json; use a fresh source directory")
    state_path = source / ".prebuild-state.json"
    if state_path.exists():
        verify_source(source, lock)
        return
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, capture=True):
        raise ValueError("Source has tracked changes; refusing to patch it")
    for patch in lock["patches"]:
        run(["git", "apply", "--check", ROOT / patch], cwd=source)
        run(["git", "apply", ROOT / patch], cwd=source)
    state = {"ref": lock["ref"], "patches": patch_records(lock),
             "diff_sha256": digest(run(["git", "diff", "--binary", "HEAD"], cwd=source, capture=True).encode())}
    write_json(state_path, state)


def verify_source(source, lock):
    state = read_json(source / ".prebuild-state.json")
    expected = {"ref": lock["ref"], "patches": patch_records(lock),
                "diff_sha256": digest(run(["git", "diff", "--binary", "HEAD"], cwd=source, capture=True).encode())}
    if state != expected or run(["git", "rev-parse", "HEAD"], cwd=source, capture=True) != lock["ref"]:
        raise ValueError("Source or patch set changed after preparation")


def cmake_definitions(config, target, arch, shared):
    definitions = {**config["common"], **config["profiles"][target["profile"]], **target["cmake"]}
    definitions["MNN_BUILD_SHARED_LIBS"] = "ON" if shared else "OFF"
    if target["os"] == "windows":
        definitions.update(MNN_WIN_RUNTIME_MT="ON", CMAKE_C_FLAGS="/D_USE_STD_VECTOR_ALGORITHMS=0",
                           CMAKE_CXX_FLAGS="/D_USE_STD_VECTOR_ALGORITHMS=0")
    if target["os"] == "macos":
        definitions.update(CMAKE_OSX_ARCHITECTURES=arch, MNN_USE_SSE="ON" if arch == "x86_64" else "OFF")
        if arch == "arm64":
            definitions["MNN_ARM82"] = "ON"
    if "cuda" in target:
        definitions["CUDA_ARCHS"] = ";".join(target["cuda"]["architectures"])
        if os.environ.get("CUDA_PATH"):
            # Legacy FindCUDA expands macro arguments as CMake source; backslashes
            # in an untyped -D cache entry would become invalid escapes such as \P.
            definitions["CUDA_TOOLKIT_ROOT_DIR"] = os.environ["CUDA_PATH"].replace("\\", "/")
        if target["os"] == "linux":
            definitions.update(CMAKE_BUILD_WITH_INSTALL_RPATH="ON", CMAKE_INSTALL_RPATH="$ORIGIN")
    if target["os"] == "android":
        ndk = os.environ.get("ANDROID_NDK_ROOT") or os.environ.get("ANDROID_NDK")
        if not ndk or not (Path(ndk) / "build/cmake/android.toolchain.cmake").is_file():
            raise ValueError("ANDROID_NDK_ROOT must point to the pinned Android NDK")
        properties = (Path(ndk) / "source.properties").read_text()
        if not re.search(r"Pkg.Revision\s*=\s*" + re.escape(target["ndk_version"]) + r"\s*(?:\n|$)", properties):
            raise ValueError("Android NDK version does not match the target configuration")
        definitions.update(CMAKE_TOOLCHAIN_FILE=str(Path(ndk).resolve() / "build/cmake/android.toolchain.cmake"),
                           ANDROID_STL="c++_static", ANDROID_NATIVE_API_LEVEL="android-21",
                           ANDROID_TOOLCHAIN="clang", MNN_BUILD_FOR_ANDROID_COMMAND="ON",
                           ANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES="ON")
    return definitions


def library_names(target, shared):
    if target["os"] == "windows":
        return [("MNN.lib", "MNN.lib"), ("MNN.dll", "MNN.dll")] if shared else [("MNN.lib", "MNN_static.lib")]
    libraries = [("libMNN" + (".dylib" if target["os"] == "macos" else ".so"),
                  "libMNN" + (".dylib" if target["os"] == "macos" else ".so"))] if shared else [("libMNN.a", "libMNN.a")]
    if "cuda" in target and target["os"] == "linux":
        libraries.append(("source/backend/cuda/libMNN_Cuda_Main.so",
                          ("" if shared else "cuda-static/") + "libMNN_Cuda_Main.so"))
    return libraries


def can_run(target, arch):
    host = {"AMD64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    arch = {"arm64": "aarch64"}.get(arch, arch)
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system())
    return target["os"] == system and (arch == host or (system == "windows" and arch == "i686" and host == "x86_64"))


def smoke(package, target, arch, shared, definitions, work):
    directory = work / ("smoke-" + arch + ("-shared" if shared else "-static"))
    # Pass toolchain/platform settings, not the MNN project's private build options.
    args = ["-D" + key + "=" + value for key, value in definitions.items()
            if key.startswith(("CMAKE_", "ANDROID_")) and key not in ("CMAKE_INSTALL_NAME_DIR", "CMAKE_BUILD_WITH_INSTALL_NAME_DIR")]
    _, config = configuration()
    backends = backend_names(config, target)
    run(["cmake", "-S", ROOT / "tests/smoke", "-B", directory, "-G", "Ninja",
         "-DMNN_DIR=" + str(package / "lib/cmake/MNN"),
         "-DMNN_REQUIRED_BACKENDS=" + ";".join(backends),
         "-DMNN_USE_STATIC_LIBS=" + ("OFF" if shared else "ON"), *args])
    run(["cmake", "--build", directory, "--parallel", "2"])
    executed = can_run(target, arch)
    backend_checks = {backend: "not_tested" for backend in backends if backend != "cpu"}
    if executed:
        env = os.environ.copy()
        if target["os"] == "windows":
            env["PATH"] = str(package / "lib") + os.pathsep + env.get("PATH", "")
        else:
            key = "DYLD_LIBRARY_PATH" if target["os"] == "macos" else "LD_LIBRARY_PATH"
            search = [str(package / "lib" / "cuda-static")] if "cuda" in target and not shared else []
            search.append(str(package / "lib"))
            if "cuda" in target and env.get("CUDA_PATH"):
                search.append(str(Path(env["CUDA_PATH"]) / "lib64"))
            env[key] = os.pathsep.join(search + [env.get(key, "")])
        executable = directory / ("mnn_smoke.exe" if target["os"] == "windows" else "mnn_smoke")
        run([executable], env=env)
        for backend in target.get("software_backend_tests", []):
            run([executable, backend], env=env)
            backend_checks[backend] = "passed_software_device"
    return {"arch": arch, "linkage": "shared" if shared else "static", "consumer_link": "passed",
            "cpu_inference": "passed" if executed else "not_run_cross_target", "gpu_inference": "not_tested",
            "backend_inference": backend_checks}


def compiler_info(directory):
    result = {}
    for file in (directory / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"):
        contents = file.read_text(encoding="utf-8")
        for name in ("CMAKE_CXX_COMPILER", "CMAKE_CXX_COMPILER_ID", "CMAKE_CXX_COMPILER_VERSION"):
            found = re.search(r'set\(' + name + r' "([^"\n]*)"\)', contents)
            if found:
                result[name] = found.group(1)
    return result


def prepare_dependency(lock, work, name):
    dependency = lock["dependencies"][name]
    archive = work / (name + ".tar.gz")
    with urllib.request.urlopen(dependency["url"], timeout=120) as response, archive.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    if file_digest(archive) != dependency["sha256"]:
        raise ValueError(name + " archive checksum mismatch")
    destination = work / "dependencies" / name
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            resolved = (destination / member.name).resolve()
            if destination.resolve() not in resolved.parents or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected entry in " + name + " archive")
        handle.extractall(destination)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("Dependency archive must contain one root directory")
    return roots[0]


def runtime_requirements(config, target):
    descriptions = {
        "vulkan": "Vulkan loader and a compatible Vulkan device/driver; dynamically loaded",
        "opencl": "OpenCL loader and a GPU OpenCL implementation; dynamically loaded",
        "opengl": "Android OpenGL ES 3.1 device, EGL and a suitable graphics context",
        "metal": "System Metal framework and a supported Apple device",
        "coreml": "System CoreML and CoreVideo frameworks; supported model operators and Apple device",
        "cuda": "External CUDA 12 runtime and cuBLAS, plus a compatible NVIDIA driver; CUDA Toolkit >=12.8,<13 needed for CMake linking",
    }
    return {backend: descriptions[backend] for backend in backend_names(config, target) if backend != "cpu"}


def write_features(directory, config, target):
    values = {"MNN_AVAILABLE_BACKENDS": ";".join(backend_names(config, target)), "MNN_PROFILE": target["profile"]}
    if "cuda" in target:
        values.update(MNN_CUDA_TOOLKIT_VERSION=target["cuda"]["version"],
                      MNN_CUDA_ARCHITECTURES=";".join(target["cuda"]["architectures"]))
    (directory / "MNNFeatures.cmake").write_text("".join('set(' + key + ' "' + value + '")\n' for key, value in values.items()))


def verify_build_options(directory, definitions):
    cache = (directory / "CMakeCache.txt").read_text(encoding="utf-8")
    for option in BACKEND_OPTIONS.values():
        found = re.search(r"^" + option + r":[^=]+=([^\n]*)$", cache, re.M)
        if not found or found.group(1) != definitions[option]:
            raise ValueError("Backend build option mismatch: " + option)


def verify_cuda_architectures(path, target):
    listing = run(["cuobjdump", "--list-elf", path], capture=True)
    actual = set(re.findall(r"sm_([0-9]+)", listing))
    expected = {arch.replace("+PTX", "").replace(".", "") for arch in target["cuda"]["architectures"]}
    if actual != expected:
        raise ValueError("CUDA cubin architectures do not match: " + repr(actual) + " expected " + repr(expected))
    ptx = run(["cuobjdump", "--list-ptx", path], capture=True)
    expected_ptx = {arch.replace("+PTX", "").replace(".", "") for arch in target["cuda"]["architectures"] if arch.endswith("+PTX")}
    actual_ptx = set(re.findall(r"(?:sm|compute)_([0-9]+)", ptx))
    if actual_ptx != expected_ptx:
        raise ValueError("CUDA PTX architectures do not match: " + repr(actual_ptx))
    return {"cubin": sorted(actual), "ptx": sorted(actual_ptx)}


def build(name, source, work, output, jobs):
    builder_sha = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
    builder_dirty = bool(run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture=True))
    lock, config = configuration()
    if name not in config["targets"]:
        raise ValueError("Unknown target: " + name)
    target = config["targets"][name]
    jobs = jobs or target.get("jobs", min(os.cpu_count() or 2, 8))
    source, work, output = source.resolve(), (work / name).resolve(), output.resolve()
    verify_source(source, lock)
    package_name = "mnn-" + lock["package_version"] + "-" + name
    package = work / package_name
    if package.exists():
        raise ValueError("Package already exists; use a new --work directory for a clean build")
    (package / "lib").mkdir(parents=True)
    shutil.copytree(source / "include/MNN", package / "include/MNN")
    shutil.copy2(source / "LICENSE.txt", package / "LICENSE-MNN.txt")
    shutil.copytree(ROOT / "patches/mnn", package / "metadata/patches")
    shutil.copy2(ROOT / "versions/mnn.json", package / "metadata/source-lock.json")
    cmake_dir = package / "lib/cmake/MNN"
    cmake_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "recipes/mnn/MNNConfig.cmake", cmake_dir)
    write_features(cmake_dir, config, target)
    dependencies = {}
    if any(arch in ("arm64", "aarch64") for arch in target["archs"]) and target["os"] != "windows":
        dependencies["kleidiai"] = prepare_dependency(lock, work, "kleidiai")
    cuda_compiler = None
    if "cuda" in target:
        dependencies["cutlass"] = prepare_dependency(lock, work, "cutlass")
        cuda_compiler = run(["nvcc", "--version"], capture=True)
        release_version = ".".join(target["cuda"]["version"].split(".")[:2])
        if "release " + release_version + "," not in cuda_compiler:
            raise ValueError("nvcc version does not match the CUDA profile")
    # Preserve notices from bundled dependencies and the pinned ARM kernels.
    for base in [source / "3rd_party", *dependencies.values()]:
        for notice in base.rglob("*"):
            if notice.is_file() and re.match(r"^(LICENSE|COPYING|COPYRIGHT|NOTICE)([._-].*)?$", notice.name, re.I):
                destination = package / "licenses" / base.name / notice.relative_to(base)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(notice, destination)
    builds, libraries = [], {}
    for arch in target["archs"]:
        for shared in ([False, True] if target["shared"] else [False]):
            definitions = cmake_definitions(config, target, arch, shared)
            if "kleidiai" in dependencies and arch in ("arm64", "aarch64"):
                definitions["KLEIDIAI_SRC_DIR"] = dependencies["kleidiai"].as_posix()
            if "cutlass" in dependencies:
                definitions["FETCHCONTENT_SOURCE_DIR_CUTLASS"] = dependencies["cutlass"].as_posix()
            directory = work / (arch + ("-shared" if shared else "-static"))
            run(["cmake", "-S", source, "-B", directory, "-G", "Ninja",
                 *["-D" + k + "=" + v for k, v in definitions.items()]])
            verify_build_options(directory, definitions)
            run(["cmake", "--build", directory, "--target", "MNN", "--parallel", str(jobs)])
            for original, packaged in library_names(target, shared):
                path = directory / original
                if not path.is_file():
                    raise ValueError("Missing build output: " + str(path))
                libraries.setdefault(packaged, []).append(path)
            result = {"arch": arch, "shared": shared, "cmake": definitions,
                      "compiler": compiler_info(directory), "backend_options": "verified"}
            if "cuda" in target:
                cuda_library = directory / ("MNN.lib" if target["os"] == "windows" and not shared else
                                            "MNN.dll" if target["os"] == "windows" else
                                            "source/backend/cuda/libMNN_Cuda_Main.so")
                result["cuda_binary_architectures"] = verify_cuda_architectures(cuda_library, target)
            builds.append(result)
    for name_on_disk, paths in libraries.items():
        destination = package / "lib" / name_on_disk
        destination.parent.mkdir(parents=True, exist_ok=True)
        if len(paths) > 1:
            run(["lipo", "-create", *paths, "-output", destination])
            actual = set(run(["lipo", "-archs", destination], capture=True).split())
            if actual != set(target["archs"]):
                raise ValueError("Universal binary architecture mismatch")
        else:
            shutil.copy2(paths[0], destination)
    if target["os"] == "windows":
        static_lib = package / "lib/MNN_static.lib"
        symbols = run(["dumpbin", "/symbols", static_lib], capture=True)
        if re.search(r"__std_(min|max)(_element)?_4", symbols):
            raise ValueError("Static library references incompatible MSVC vector helpers")
        if re.search(rb"[A-Za-z0-9_.-]+\.pdb", static_lib.read_bytes(), re.I):
            raise ValueError("Static library references an external compiler PDB")
    checks = [smoke(package, target, item["arch"], item["shared"], item["cmake"], work) for item in builds]
    manifest = {"schema_version": 1, "component": "mnn", "package": package_name,
                "source": lock, "patches": patch_records(lock), "target": name,
                "platform": target, "backends": backend_names(config, target),
                "runtime_requirements": runtime_requirements(config, target),
                "builder_sha": builder_sha,
                "builder_dirty": builder_dirty or builder_sha != run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
                                 or bool(run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture=True)),
                "environment": {"host": platform.platform(), "python": sys.version,
                                "cmake": run(["cmake", "--version"], capture=True).splitlines()[0],
                                "cuda_compiler": cuda_compiler,
                                "runner_image": os.environ.get("ImageVersion", "local")},
                "builds": builds, "validation": checks,
                "files": {str(path.relative_to(package)).replace("\\", "/"): file_digest(path)
                          for path in sorted(package.rglob("*")) if path.is_file()}}
    write_json(package / "manifest.json", manifest)
    output.mkdir(parents=True, exist_ok=True)
    if target["os"] == "windows":
        archive = output / (package_name + ".zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    handle.write(path, path.relative_to(package.parent))
    else:
        archive = output / (package_name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(package, arcname=package_name)
    shutil.copy2(package / "manifest.json", output / (package_name + ".manifest.json"))
    verify_archive(archive, manifest)
    (output / (archive.name + ".sha256")).write_text(file_digest(archive) + "  " + archive.name + "\n")
    print("Verified package: " + str(archive), flush=True)


def verify_archive(path, manifest):
    prefix = manifest["package"] + "/"
    expected = {prefix + name: value for name, value in manifest["files"].items()}
    hashes, names = {}, []
    embedded = None
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as handle:
            for item in handle.infolist():
                if item.is_dir():
                    continue
                names.append(item.filename)
                with handle.open(item) as contents:
                    if item.filename == prefix + "manifest.json":
                        embedded = json.load(contents)
                    else:
                        hashes[item.filename] = stream_digest(contents)
    else:
        with tarfile.open(path, "r:gz") as handle:
            entries = handle.getmembers()
            if any(not item.isfile() and not item.isdir() for item in entries):
                raise ValueError("Archive contains a non-regular entry")
            for item in entries:
                if not item.isfile():
                    continue
                names.append(item.name)
                with handle.extractfile(item) as contents:
                    if item.name == prefix + "manifest.json":
                        embedded = json.load(contents)
                    else:
                        hashes[item.name] = stream_digest(contents)
    if len(set(names)) != len(names) or set(names) != set(expected) | {prefix + "manifest.json"}:
        raise ValueError("Archive file list does not match manifest")
    if embedded != manifest:
        raise ValueError("Embedded manifest does not match sidecar")
    for name, expected_hash in expected.items():
        if hashes[name] != expected_hash:
            raise ValueError("Checksum mismatch: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--target", default="all")
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, default=ROOT / ".work/source")
    compile_parser = commands.add_parser("build")
    compile_parser.add_argument("--target", required=True)
    compile_parser.add_argument("--source", type=Path, default=ROOT / ".work/source")
    compile_parser.add_argument("--work", type=Path, default=ROOT / ".work/build")
    compile_parser.add_argument("--output", type=Path, default=ROOT / "dist")
    compile_parser.add_argument("--jobs", type=int, help="Override the target's build parallelism")
    args = parser.parse_args()
    if args.command == "plan":
        lock, planned = matrix(args.target)
        print(json.dumps(planned, separators=(",", ":")))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                handle.write("matrix=" + json.dumps(planned, separators=(",", ":")) + "\n")
                handle.write("ref=" + lock["ref"] + "\nrelease_tag=" + lock["release_tag"] + "\n")
    elif args.command == "prepare":
        prepare(args.source)
    else:
        build(args.target, args.source, args.work, args.output, args.jobs)


if __name__ == "__main__":
    main()
