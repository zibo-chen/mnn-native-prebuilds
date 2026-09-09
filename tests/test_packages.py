import json
import shutil
import subprocess
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "recipes/mnn"))
sys.path.insert(0, str(ROOT / "scripts"))
import build
import release

BUILDER_SHA = "a" * 40


def fixture(directory, target_name):
    lock, config = build.configuration()
    target = config["targets"][target_name]
    package = "mnn-" + lock["package_version"] + "-" + target_name
    files = {"include/MNN/Interpreter.hpp": b"header", "include/MNN/expr/ExprCreator.hpp": b"expr",
             "lib/cmake/MNN/MNNConfig.cmake": b"cmake", "lib/cmake/MNN/MNNFeatures.cmake": b"features"}
    for shared in ([False, True] if target["shared"] else [False]):
        for _, name in build.library_names(target, shared):
            files["lib/" + name] = b"fixture library"
    manifest = {"target": target_name, "source": lock, "patches": build.patch_records(lock),
                "package": package, "platform": target, "builder_sha": BUILDER_SHA, "builder_dirty": False,
                "backends": build.backend_names(config, target), "runtime_requirements": build.runtime_requirements(config, target),
                "files": {name: build.digest(data) for name, data in files.items()},
                "builds": [{"arch": arch, "shared": shared, "backend_options": "verified",
                            "cmake": {**config["common"], **config["profiles"][target["profile"]], **target["cmake"]},
                            **({"cuda_binary_architectures": {"cubin": ["120", "75", "80", "86", "89", "90"], "ptx": ["120"]}}
                               if "cuda" in target else {})}
                           for arch in target["archs"] for shared in ([False, True] if target["shared"] else [False])],
                "validation": [{"arch": arch, "linkage": linkage, "consumer_link": "passed",
                                "cpu_inference": "not_run_cross_target", "gpu_inference": "not_tested",
                                "backend_inference": {name: "passed_software_device" for name in target.get("software_backend_tests", [])}}
                               for arch in target["archs"]
                               for linkage in (["static", "shared"] if target["shared"] else ["static"])]}
    archive = directory / (package + (".zip" if target["os"] == "windows" else ".tar.gz"))
    files["manifest.json"] = json.dumps(manifest).encode()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive, "w") as handle:
            for name, data in files.items():
                handle.writestr(package + "/" + name, data)
    else:
        import io
        with tarfile.open(archive, "w:gz") as handle:
            for name, data in files.items():
                info = tarfile.TarInfo(package + "/" + name)
                info.size = len(data)
                handle.addfile(info, io.BytesIO(data))
    build.write_json(directory / (package + ".manifest.json"), manifest)
    (directory / (archive.name + ".sha256")).write_text(build.digest(archive.read_bytes()) + "  " + archive.name + "\n")
    return archive, manifest


class PackageContracts(unittest.TestCase):
    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ValueError):
            build.matrix("linux-x86_64; echo injected")

    def test_floating_upstream_ref_is_rejected(self):
        lock, config = build.configuration()
        lock["ref"] = "master"
        with patch.object(build, "read_json", side_effect=[lock, config]):
            with self.assertRaisesRegex(ValueError, "full commit SHA"):
                build.configuration()

    def test_existing_ten_platform_contracts_are_preserved(self):
        _, matrix = build.matrix("all")
        self.assertTrue({item["target"] for item in matrix["include"]}.issuperset({
            "linux-x86_64", "linux-aarch64", "windows-x86_64", "windows-i686", "windows-aarch64",
            "macos-universal", "ios-arm64", "ios-arm64-sim", "android-arm64-v8a", "android-armeabi-v7a"}))

    def test_backend_variants_preserve_base_abi(self):
        _, config = build.configuration()
        _, matrix = build.matrix("backends")
        self.assertEqual(len(matrix["include"]), 12)
        for target in matrix["include"]:
            base = config["targets"][target["base"]]
            for key in ("archs", "abi", "runtime", "minimum_os", "shared"):
                self.assertEqual(target[key], base[key])
        self.assertEqual(build.backend_names(config, config["targets"]["android-arm64-v8a-vulkan-opencl-opengl"]),
                         ["cpu", "vulkan", "opencl", "opengl"])

    def test_linux_cuda_static_package_has_companion_library(self):
        _, config = build.configuration()
        target = config["targets"]["linux-x86_64-cuda12"]
        self.assertIn(("source/backend/cuda/libMNN_Cuda_Main.so", "cuda-static/libMNN_Cuda_Main.so"),
                      build.library_names(target, False))

    @unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
    def test_cmake_rejects_missing_required_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "sdk/lib/cmake/MNN"
            package.mkdir(parents=True)
            shutil.copy2(ROOT / "recipes/mnn/MNNConfig.cmake", package)
            (package / "MNNFeatures.cmake").write_text('set(MNN_AVAILABLE_BACKENDS "cpu")\nset(MNN_PROFILE "cpu")\n')
            (root / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.24)\nproject(test NONE)\nfind_package(MNN REQUIRED CONFIG COMPONENTS vulkan)\n')
            result = subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build"), "-DMNN_DIR=" + str(package)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Backend vulkan is unavailable", result.stdout)

    def test_device_and_simulator_are_distinct(self):
        _, config = build.configuration()
        self.assertEqual(config["targets"]["ios-arm64"]["cmake"]["CMAKE_OSX_SYSROOT"], "iphoneos")
        self.assertEqual(config["targets"]["ios-arm64-sim"]["cmake"]["CMAKE_OSX_SYSROOT"], "iphonesimulator")

    def test_cross_target_is_not_reported_as_executed(self):
        _, config = build.configuration()
        with patch.object(build.platform, "system", return_value="Linux"), patch.object(build.platform, "machine", return_value="x86_64"):
            self.assertFalse(build.can_run(config["targets"]["linux-aarch64"], "aarch64"))
            self.assertFalse(build.can_run(config["targets"]["android-arm64-v8a"], "aarch64"))
            self.assertTrue(build.can_run(config["targets"]["linux-x86_64"], "x86_64"))

    def test_tar_and_zip_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            for target in ("linux-x86_64", "windows-x86_64"):
                archive, manifest = fixture(Path(temporary), target)
                build.verify_archive(archive, manifest)

    def test_archive_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive, manifest = fixture(Path(temporary), "windows-x86_64")
            with zipfile.ZipFile(archive) as handle:
                files = {name: handle.read(name) for name in handle.namelist()}
            files[manifest["package"] + "/lib/MNN.dll"] = b"corrupted"
            with zipfile.ZipFile(archive, "w") as handle:
                for name, data in files.items():
                    handle.writestr(name, data)
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                build.verify_archive(archive, manifest)

    def test_incomplete_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            fixture(Path(temporary), "linux-x86_64")
            with self.assertRaisesRegex(ValueError, "Missing targets"):
                release.assemble(Path(temporary))

    def test_mixed_builder_commits_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value="b" * 40):
            fixture(Path(temporary), "linux-x86_64")
            with self.assertRaisesRegex(ValueError, "clean builder commit"):
                release.assemble(Path(temporary), "linux-x86_64")

    def test_missing_link_validation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            directory = Path(temporary)
            _, manifest = fixture(directory, "linux-x86_64")
            manifest["validation"].pop()
            build.write_json(directory / (manifest["package"] + ".manifest.json"), manifest)
            with self.assertRaisesRegex(ValueError, "Missing consumer validation"):
                release.assemble(directory, "linux-x86_64")

    def test_missing_cuda_companion_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            directory = Path(temporary)
            _, manifest = fixture(directory, "linux-x86_64-cuda12")
            del manifest["files"]["lib/cuda-static/libMNN_Cuda_Main.so"]
            build.write_json(directory / (manifest["package"] + ".manifest.json"), manifest)
            with self.assertRaisesRegex(ValueError, "Required SDK files"):
                release.assemble(directory, "linux-x86_64-cuda12")

    def test_missing_cuda_architecture_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            directory = Path(temporary)
            _, manifest = fixture(directory, "windows-x86_64-cuda12")
            manifest["builds"][0]["cuda_binary_architectures"]["cubin"].remove("89")
            build.write_json(directory / (manifest["package"] + ".manifest.json"), manifest)
            with self.assertRaisesRegex(ValueError, "CUDA binary architecture validation"):
                release.assemble(directory, "windows-x86_64-cuda12")

    def test_missing_software_vulkan_test_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            directory = Path(temporary)
            _, manifest = fixture(directory, "linux-x86_64-vulkan-opencl")
            manifest["validation"][0]["backend_inference"]["vulkan"] = "not_tested"
            build.write_json(directory / (manifest["package"] + ".manifest.json"), manifest)
            with self.assertRaisesRegex(ValueError, "Missing software backend inference"):
                release.assemble(directory, "linux-x86_64-vulkan-opencl")

    def test_complete_release_produces_index_and_checksums(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, "run", return_value=BUILDER_SHA):
            directory = Path(temporary)
            _, config = build.configuration()
            for name in config["targets"]:
                fixture(directory, name)
            index = release.assemble(directory)
            self.assertEqual(len(index["packages"]), 22)
            self.assertEqual(len((directory / "SHA256SUMS").read_text().splitlines()), 22)
            self.assertEqual(build.read_json(directory / "index.json"), index)


if __name__ == "__main__":
    unittest.main()
