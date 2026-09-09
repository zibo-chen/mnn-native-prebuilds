#!/usr/bin/env python3
"""Discover stable upstream tags, pin a build commit and dispatch once per commit."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "recipes/mnn"))
import build

STABLE_VERSION = re.compile(r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))")
ACTIVE_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}


def version_tuple(version):
    return tuple(map(int, version.split(".")))


def parse_tags(output):
    refs = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            continue
        refs[fields[1]] = fields[0]
    versions = {}
    for ref, sha in refs.items():
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        match = STABLE_VERSION.fullmatch(name)
        if not match:
            continue
        version = match.group(1)
        # Annotated tags point to a tag object; use the peeled commit instead.
        sha = refs.get(ref + "^{}", sha)
        tag = {"name": name, "version": version, "ref": sha}
        previous = versions.get(version)
        if previous and previous["ref"] != sha:
            raise ValueError("Conflicting upstream tags for version " + version)
        if not previous or not name.startswith("v"):
            versions[version] = tag
    return sorted(versions.values(), key=lambda tag: version_tuple(tag["version"]))


def lock_for_tag(lock, tag):
    updated = copy.deepcopy(lock)
    package_version = tag["version"] + "-" + tag["ref"][:8] + "-r1"
    updated.update(version=tag["version"], ref=tag["ref"], upstream_tag=tag["name"], revision=1,
                   package_version=package_version, release_tag="mnn-" + package_version,
                   note="Official upstream tag " + tag["name"] + ", pinned to commit " + tag["ref"] + ".")
    return updated


def build_title(sha):
    return "MNN all · publish=true · " + sha


def choose_build(lock, tags, releases, runs, builder_sha):
    """Pure selection logic. Failed attempts wait for a builder fix or manual rerun."""
    published = {release["tag_name"] for release in releases if not release["draft"]}
    drafts = {release["tag_name"] for release in releases if release["draft"]}
    current_tag = lock.get("upstream_tag")
    attempts = [run for run in runs if run.get("display_title") == build_title(builder_sha)]
    attempt = max(attempts, key=lambda run: run["id"], default=None)
    if current_tag:
        current = next((tag for tag in tags if tag["version"] == lock["version"]), None)
        if current is None or current["ref"] != lock["ref"]:
            raise ValueError("The locked upstream tag was deleted or moved: " + current_tag)
        if lock["release_tag"] in drafts:
            raise ValueError("Existing draft release needs attention: " + lock["release_tag"])
        if lock["release_tag"] not in published:
            if attempt is None:
                return {"action": "build", "lock": lock, "reason": "Resume the unpublished pinned version"}
            if attempt["status"] in ACTIVE_STATES:
                return {"action": "none", "reason": "The pinned version is already building", "run_url": attempt["html_url"]}
    # Process tags in version order, including tags without a GitHub Release object.
    for tag in tags:
        if version_tuple(tag["version"]) <= version_tuple(lock["version"]):
            continue
        candidate = lock_for_tag(lock, tag)
        if candidate["release_tag"] in drafts:
            raise ValueError("Existing draft release needs attention: " + candidate["release_tag"])
        if candidate["release_tag"] not in published:
            return {"action": "build", "lock": candidate, "reason": "New stable upstream tag " + tag["name"]}
    reason = "No unpublished newer stable tag"
    if current_tag and lock["release_tag"] not in published and attempt:
        reason = "This builder commit already attempted the version; fix the builder or manually rerun the failed build"
    return {"action": "none", "reason": reason}


def github(path, paginate=False):
    args = ["gh", "api", path]
    if paginate:
        args += ["--paginate", "--slurp"]
    # API errors are failures, never interpreted as no tags, releases or builds.
    result = json.loads(build.run(args, cwd=ROOT, capture=True))
    return result


def inspect_upstream(repository):
    lock, _ = build.configuration()
    remote = build.run(["git", "ls-remote", "--tags", "https://github.com/" + lock["repository"] + ".git"], capture=True)
    tags = parse_tags(remote)
    if not tags:
        raise ValueError("Upstream returned no stable version tags")
    releases = [item for page in github("repos/" + repository + "/releases?per_page=100", True) for item in page]
    run_pages = github("repos/" + repository + "/actions/workflows/build.yml/runs?event=workflow_dispatch&per_page=100", True)
    runs = [item for page in run_pages for item in page["workflow_runs"]]
    sha = build.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
    return lock, sha, choose_build(lock, tags, releases, runs, sha)


def execute(repository, original_lock, original_sha, plan):
    branch = github("repos/" + repository)["default_branch"]
    if build.run(["git", "status", "--porcelain"], cwd=ROOT, capture=True):
        raise ValueError("The updater requires a clean checkout")
    remote_sha = build.run(["git", "ls-remote", "origin", "refs/heads/" + branch], cwd=ROOT, capture=True).split()[0]
    if remote_sha != original_sha:
        raise ValueError("Default branch changed; rerun the checker on the latest commit")
    updated = plan["lock"]
    # Check every compatibility patch before advancing the repository's source lock.
    source = ROOT / ".work/upstream" / updated["ref"]
    build.prepare(source, updated)
    definitions = (source / "include/MNN/MNNDefine.h").read_text(encoding="utf-8")
    source_version = ".".join(re.search(r"#define MNN_VERSION_" + part + r"\s+([0-9]+)", definitions).group(1)
                              for part in ("MAJOR", "MINOR", "PATCH"))
    if source_version != updated["version"]:
        raise ValueError("Upstream version macros do not match tag " + updated["upstream_tag"])
    builder_sha = original_sha
    if updated != original_lock:
        build.write_json(ROOT / "versions/mnn.json", updated)
        build.configuration()
        build.run(["git", "add", "versions/mnn.json"], cwd=ROOT)
        build.run(["git", "-c", "user.name=github-actions[bot]", "-c",
                   "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m",
                   "Pin MNN " + updated["upstream_tag"] + " for automatic prebuilds"], cwd=ROOT)
        builder_sha = build.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True)
        # No force push; concurrent changes safely fail instead of being overwritten.
        build.run(["git", "push", "origin", "HEAD:refs/heads/" + branch], cwd=ROOT)
    # GITHUB_TOKEN pushes do not trigger push workflows. Dispatch explicitly;
    # builder_ref pins all plan/build/release checkouts even if main moves again.
    build.run(["gh", "workflow", "run", "build.yml", "--repo", repository, "--ref", branch,
               "-f", "target=all", "-F", "publish=true", "-f", "builder_ref=" + builder_sha], cwd=ROOT)
    return builder_sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GH_REPO", "zibo-chen/mnn-native-prebuilds"))
    parser.add_argument("--apply", action="store_true", help="Commit the source lock and dispatch; default is read-only")
    args = parser.parse_args()
    lock, sha, plan = inspect_upstream(args.repo)
    print(json.dumps(plan, indent=2))
    summary = plan["reason"]
    if plan["action"] == "build":
        summary += "\n\nRelease: `" + plan["lock"]["release_tag"] + "`; upstream SHA: `" + plan["lock"]["ref"] + "`."
        if args.apply:
            builder_sha = execute(args.repo, lock, sha, plan)
            summary += "\n\nDispatched the complete matrix with publishing enabled. Builder commit: `" + builder_sha + "`."
        else:
            summary += "\n\nDry run: no source lock, repository or workflow changes."
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    print(summary)


if __name__ == "__main__":
    main()
