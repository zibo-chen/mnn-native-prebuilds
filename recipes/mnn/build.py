#!/usr/bin/env python3
"""Pinned MNN source preparation, cross-platform builds and package verification."""
import argparse
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


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


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
    for name, target in config["targets"].items():
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Invalid target name")
        if target["profile"] not in config["profiles"]:
            raise ValueError("Unknown profile for " + name)
        if target["profile"] == "metal" and target["os"] not in ("macos", "ios"):
            raise ValueError("Metal requires an Apple target")
    return lock, config


def matrix(selection):
    lock, config = configuration()
    names = list(config["targets"]) if selection == "all" else [selection]
    if any(name not in config["targets"] for name in names):
        raise ValueError("Unknown target: " + selection)
    return lock, {"include": [dict(target=name, **config["targets"][name]) for name in names]}


def patch_records(lock):
    return [{"path": name, "sha256": digest((ROOT / name).read_bytes())}
            for name in lock["patches"]]


def prepare(source):
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
    return [("libMNN" + (".dylib" if target["os"] == "macos" else ".so"),
             "libMNN" + (".dylib" if target["os"] == "macos" else ".so"))] if shared else [("libMNN.a", "libMNN.a")]


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
    run(["cmake", "-S", ROOT / "tests/smoke", "-B", directory, "-G", "Ninja",
         "-DMNN_DIR=" + str(package / "lib/cmake/MNN"),
         "-DMNN_USE_STATIC_LIBS=" + ("OFF" if shared else "ON"), *args])
    run(["cmake", "--build", directory, "--parallel", "2"])
    executed = can_run(target, arch)
    if executed:
        env = os.environ.copy()
        if target["os"] == "windows":
            env["PATH"] = str(package / "lib") + os.pathsep + env.get("PATH", "")
        else:
            key = "DYLD_LIBRARY_PATH" if target["os"] == "macos" else "LD_LIBRARY_PATH"
            env[key] = str(package / "lib") + os.pathsep + env.get(key, "")
        run([directory / ("mnn_smoke.exe" if target["os"] == "windows" else "mnn_smoke")], env=env)
    return {"arch": arch, "linkage": "shared" if shared else "static", "consumer_link": "passed",
            "cpu_inference": "passed" if executed else "not_run_cross_target", "gpu_inference": "not_tested"}


def compiler_info(directory):
    result = {}
    for file in (directory / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"):
        contents = file.read_text(encoding="utf-8")
        for name in ("CMAKE_CXX_COMPILER", "CMAKE_CXX_COMPILER_ID", "CMAKE_CXX_COMPILER_VERSION"):
            found = re.search(r'set\(' + name + r' "([^"\n]*)"\)', contents)
            if found:
                result[name] = found.group(1)
    return result


def prepare_kleidiai(lock, work):
    dependency = lock["dependencies"]["kleidiai"]
    archive = work / "kleidiai.tar.gz"
    with urllib.request.urlopen(dependency["url"], timeout=120) as response:
        contents = response.read()
    if digest(contents) != dependency["sha256"]:
        raise ValueError("KleidiAI archive checksum mismatch")
    archive.write_bytes(contents)
    destination = work / "dependencies"
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            resolved = (destination / member.name).resolve()
            if destination.resolve() not in resolved.parents or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected entry in KleidiAI archive")
        handle.extractall(destination)
    return destination / ("kleidiai-" + dependency["version"])


def build(name, source, work, output, jobs):
    lock, config = configuration()
    if name not in config["targets"]:
        raise ValueError("Unknown target: " + name)
    target = config["targets"][name]
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
    dependencies = []
    if any(arch in ("arm64", "aarch64") for arch in target["archs"]) and target["os"] != "windows":
        dependencies.append(prepare_kleidiai(lock, work))
    # Preserve notices from bundled dependencies and the pinned ARM kernels.
    for base in [source / "3rd_party", *dependencies]:
        for notice in base.rglob("*"):
            if notice.is_file() and re.match(r"^(LICENSE|COPYING|COPYRIGHT|NOTICE)([._-].*)?$", notice.name, re.I):
                destination = package / "licenses" / base.name / notice.relative_to(base)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(notice, destination)
    builds, libraries = [], {}
    for arch in target["archs"]:
        for shared in ([False, True] if target["shared"] else [False]):
            definitions = cmake_definitions(config, target, arch, shared)
            if dependencies and arch in ("arm64", "aarch64"):
                definitions["KLEIDIAI_SRC_DIR"] = str(dependencies[0])
            directory = work / (arch + ("-shared" if shared else "-static"))
            run(["cmake", "-S", source, "-B", directory, "-G", "Ninja",
                 *["-D" + k + "=" + v for k, v in definitions.items()]])
            run(["cmake", "--build", directory, "--target", "MNN", "--parallel", str(jobs)])
            for original, packaged in library_names(target, shared):
                path = directory / original
                if not path.is_file():
                    raise ValueError("Missing build output: " + str(path))
                libraries.setdefault(packaged, []).append(path)
            builds.append({"arch": arch, "shared": shared, "cmake": definitions,
                           "compiler": compiler_info(directory)})
    for name_on_disk, paths in libraries.items():
        destination = package / "lib" / name_on_disk
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
                "platform": target, "backends": ["cpu"] + (["metal"] if target["profile"] == "metal" else []),
                "builder_sha": run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True),
                "builder_dirty": bool(run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture=True)),
                "environment": {"host": platform.platform(), "python": sys.version,
                                "cmake": run(["cmake", "--version"], capture=True).splitlines()[0],
                                "runner_image": os.environ.get("ImageVersion", "local")},
                "builds": builds, "validation": checks,
                "files": {str(path.relative_to(package)).replace("\\", "/"): digest(path.read_bytes())
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
    (output / (archive.name + ".sha256")).write_text(digest(archive.read_bytes()) + "  " + archive.name + "\n")
    print("Verified package: " + str(archive), flush=True)


def verify_archive(path, manifest):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as handle:
            names = [item.filename for item in handle.infolist() if not item.is_dir()]
            contents = {name: handle.read(name) for name in names}
    else:
        with tarfile.open(path, "r:gz") as handle:
            entries = handle.getmembers()
            if any(not item.isfile() and not item.isdir() for item in entries):
                raise ValueError("Archive contains a non-regular entry")
            names = [item.name for item in entries if item.isfile()]
            contents = {item.name: handle.extractfile(item).read() for item in entries if item.isfile()}
    prefix = manifest["package"] + "/"
    expected = {prefix + name: value for name, value in manifest["files"].items()}
    if len(set(names)) != len(names) or set(contents) != set(expected) | {prefix + "manifest.json"}:
        raise ValueError("Archive file list does not match manifest")
    if json.loads(contents[prefix + "manifest.json"]) != manifest:
        raise ValueError("Embedded manifest does not match sidecar")
    for name, expected_hash in expected.items():
        if digest(contents[name]) != expected_hash:
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
    compile_parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
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
