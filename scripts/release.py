#!/usr/bin/env python3
"""Verify the complete matrix, generate the release index, optionally publish."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "recipes/mnn"))
import build


def assemble(directory, selection="all"):
    lock, config = build.configuration()
    _, matrix = build.matrix(selection)
    expected_targets = {item["target"] for item in matrix["include"]}
    manifests = list(directory.glob("*.manifest.json"))
    packages = []
    seen = set()
    builder_sha = build.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
    for sidecar in sorted(manifests):
        manifest = build.read_json(sidecar)
        target_name = manifest["target"]
        if target_name not in expected_targets or target_name in seen:
            raise ValueError("Unexpected or duplicate target: " + target_name)
        target = config["targets"][target_name]
        package_name = "mnn-" + lock["package_version"] + "-" + target_name
        if manifest["source"] != lock or manifest["patches"] != build.patch_records(lock):
            raise ValueError("Source lock or patch mismatch: " + target_name)
        if manifest["platform"] != target or manifest["package"] != package_name:
            raise ValueError("Package configuration mismatch: " + target_name)
        if manifest["backends"] != build.backend_names(config, target) or manifest["runtime_requirements"] != build.runtime_requirements(config, target):
            raise ValueError("Backend metadata mismatch: " + target_name)
        if manifest["builder_sha"] != builder_sha or manifest["builder_dirty"]:
            raise ValueError("Package was not built from the current clean builder commit")
        expected_checks = {(arch, linkage) for arch in target["archs"]
                           for linkage in (["static", "shared"] if target["shared"] else ["static"])}
        checks = manifest["validation"]
        if {(item["arch"], item["linkage"]) for item in checks} != expected_checks or len(checks) != len(expected_checks):
            raise ValueError("Missing consumer validation: " + target_name)
        if any(item["consumer_link"] != "passed" or item["cpu_inference"] not in ("passed", "not_run_cross_target") for item in checks):
            raise ValueError("Consumer validation failed: " + target_name)
        for item in checks:
            if any(item.get("backend_inference", {}).get(backend) != "passed_software_device"
                   for backend in target.get("software_backend_tests", [])):
                raise ValueError("Missing software backend inference validation: " + target_name)
        builds = manifest.get("builds", [])
        if {(item["arch"], "shared" if item["shared"] else "static") for item in builds} != expected_checks or len(builds) != len(expected_checks):
            raise ValueError("Missing build evidence: " + target_name)
        for item in builds:
            if item.get("backend_options") != "verified":
                raise ValueError("Missing backend build option validation: " + target_name)
            options = {**config["common"], **config["profiles"][target["profile"]], **target["cmake"]}
            if any(item["cmake"].get(option) != options[option] for option in build.BACKEND_OPTIONS.values()):
                raise ValueError("Backend build option mismatch: " + target_name)
            if "cuda" in target:
                archs = target["cuda"]["architectures"]
                expected_cuda = {"cubin": sorted({arch.replace("+PTX", "").replace(".", "") for arch in archs}),
                                 "ptx": sorted({arch.replace("+PTX", "").replace(".", "") for arch in archs if arch.endswith("+PTX")})}
                if item.get("cuda_binary_architectures") != expected_cuda:
                    raise ValueError("Missing CUDA binary architecture validation: " + target_name)
        required = {"include/MNN/Interpreter.hpp", "include/MNN/expr/ExprCreator.hpp",
                    "lib/cmake/MNN/MNNConfig.cmake", "lib/cmake/MNN/MNNFeatures.cmake"}
        for shared in ([False, True] if target["shared"] else [False]):
            required.update("lib/" + packaged for _, packaged in build.library_names(target, shared))
        if not required.issubset(manifest["files"]):
            raise ValueError("Required SDK files are missing: " + target_name)
        archive = directory / (package_name + (".zip" if target["os"] == "windows" else ".tar.gz"))
        build.verify_archive(archive, manifest)
        checksum = build.file_digest(archive)
        if (directory / (archive.name + ".sha256")).read_text().strip() != checksum + "  " + archive.name:
            raise ValueError("Archive checksum sidecar mismatch: " + target_name)
        seen.add(target_name)
        packages.append({"file": archive.name, "sha256": checksum, "size": archive.stat().st_size,
                         "manifest": sidecar.name, "target": target_name, "profile": target["profile"],
                         "abi": target["abi"], "backends": manifest["backends"], "validation": checks,
                         "runtime_requirements": manifest["runtime_requirements"], "cuda": target.get("cuda")})
    if seen != expected_targets:
        raise ValueError("Missing targets: " + ", ".join(sorted(expected_targets - seen)))
    index = {"schema_version": 1, "release_tag": lock["release_tag"], "source": lock,
             "builder_sha": builder_sha, "packages": packages}
    build.write_json(directory / "index.json", index)
    (directory / "SHA256SUMS").write_text("".join(item["sha256"] + "  " + item["file"] + "\n" for item in packages))
    notes = ["MNN native prebuilts " + lock["package_version"], "",
             "Upstream: [alibaba/MNN@" + lock["ref"][:8] + "](https://github.com/alibaba/MNN/commit/" + lock["ref"] + ").",
             "", "".join(["Source version: ", lock["version"], ". Packaging revision: ", str(lock["revision"]), "."]),
             lock["note"], "",
             "Each SDK contains libraries, headers, a relocatable CMake package, licenses and a manifest.",
             "All published static/shared variants passed consumer linking. Native desktop variants also passed CPU inference.",
             "The Linux x86_64 Vulkan profile also passed inference on a Mesa software Vulkan device.",
             "Physical GPU/NPU inference and cross-target inference are not validated by this workflow.", "",
             "| Target | Profile | ABI |", "|---|---|---|"]
    notes += ["| " + item["target"] + " | " + item["profile"] + " | " + item["abi"] + " |" for item in packages]
    notes += ["", "See the README for minimum operating systems, runtimes and migration instructions.",
              "CUDA 12 packages require external CUDA runtime/cuBLAS and NVIDIA drivers. CMake linking requires Toolkit >=12.8,<13.",
              "CUDA Toolkit 12.8.1; cubins: SM 75, 80, 86, 89, 90, 120; PTX: compute 120. Linux static MNN still needs lib/cuda-static/libMNN_Cuda_Main.so.",
              "OpenCV and MNN CV are separate features and are not included in these inference packages.", ""]
    (directory / "release-notes.md").write_text("\n".join(notes))
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist")
    parser.add_argument("--target", default="all")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and args.target != "all":
        parser.error("Publishing requires the complete target matrix")
    index = assemble(args.directory, args.target)
    if args.publish:
        files = [str(path) for path in sorted(args.directory.iterdir()) if path.is_file() and path.name != "release-notes.md"]
        # gh fails on an existing release; never overwrite previously published packages.
        subprocess.run(["gh", "release", "create", index["release_tag"], "--target", index["builder_sha"],
                        "--title", "MNN " + index["source"]["package_version"], "--notes-file",
                        str(args.directory / "release-notes.md"), *files], cwd=ROOT, check=True)
    print("Verified " + str(len(index["packages"])) + " packages")


if __name__ == "__main__":
    main()
