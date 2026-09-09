import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_upstream as updater

SHA = "a" * 40
NEW_SHA = "b" * 40
BUILDER = "c" * 40


def original_lock():
    return {"repository": "alibaba/MNN", "version": "3.6.0", "ref": SHA,
            "revision": 2, "package_version": "3.6.0-aaaaaaaa-r2",
            "release_tag": "mnn-3.6.0-aaaaaaaa-r2", "patches": ["compat.patch"],
            "dependencies": {"cutlass": {"sha256": "d" * 64}}}


def tag(version="3.6.1", sha=NEW_SHA):
    return {"name": version, "version": version, "ref": sha}


def attempt(status="in_progress", conclusion="", builder=BUILDER):
    return {"id": 42, "display_title": updater.build_title(builder), "status": status,
            "conclusion": conclusion, "html_url": "https://github.com/example/actions/runs/42"}


class UpstreamSelection(unittest.TestCase):
    def test_tag_only_release_and_annotated_tag_peeling(self):
        tags = updater.parse_tags("\n".join([
            SHA + "\trefs/tags/v3.6.1", NEW_SHA + "\trefs/tags/v3.6.1^{}",
            SHA + "\trefs/tags/3.6.2-rc1", SHA + "\trefs/tags/Android-0.8.1.2",
            SHA + "\trefs/tags/3.6.3;touch-bad", SHA + "\trefs/heads/3.6.4"]))
        self.assertEqual(tags, [{"name": "v3.6.1", "version": "3.6.1", "ref": NEW_SHA}])
        plan = updater.choose_build(original_lock(), tags, [], [], BUILDER)
        self.assertEqual(plan["lock"]["ref"], NEW_SHA)
        self.assertEqual(plan["lock"]["upstream_tag"], "v3.6.1")

    def test_conflicting_version_aliases_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            updater.parse_tags(SHA + "\trefs/tags/3.6.1\n" + NEW_SHA + "\trefs/tags/v3.6.1")

    def test_semantic_order_does_not_skip_multiple_new_tags(self):
        lock = original_lock()
        lock["version"] = "3.8.0"
        tags = updater.parse_tags(SHA + "\trefs/tags/3.10.0\n" + NEW_SHA + "\trefs/tags/3.9.0")
        plan = updater.choose_build(lock, tags, [], [], BUILDER)
        self.assertEqual(plan["lock"]["version"], "3.9.0")

    def test_new_lock_preserves_dependencies_and_resets_packaging_revision(self):
        lock = original_lock()
        saved = copy.deepcopy(lock)
        updated = updater.lock_for_tag(lock, tag())
        self.assertEqual(lock, saved)
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["release_tag"], "mnn-3.6.1-bbbbbbbb-r1")
        self.assertEqual(updated["dependencies"], lock["dependencies"])
        self.assertEqual(updated["patches"], lock["patches"])

    def test_published_version_is_not_dispatched_again(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        plan = updater.choose_build(lock, [tag()], [{"tag_name": lock["release_tag"], "draft": False}], [], BUILDER)
        self.assertEqual(plan["action"], "none")

    def test_resume_after_commit_but_before_dispatch(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        plan = updater.choose_build(lock, [tag()], [], [], BUILDER)
        self.assertEqual(plan["action"], "build")
        self.assertEqual(plan["lock"], lock)

    def test_running_build_is_not_duplicated(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        for status in updater.ACTIVE_STATES:
            plan = updater.choose_build(lock, [tag(), tag("3.6.2")], [], [attempt(status)], BUILDER)
            self.assertEqual(plan["action"], "none")

    def test_failed_build_does_not_repeat_until_builder_changes(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        failed = attempt("completed", "failure")
        self.assertEqual(updater.choose_build(lock, [tag()], [], [failed], BUILDER)["action"], "none")
        self.assertEqual(updater.choose_build(lock, [tag()], [], [failed], "e" * 40)["action"], "build")

    def test_failed_older_version_does_not_block_a_new_tag(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        plan = updater.choose_build(lock, [tag(), tag("3.6.2", "f" * 40)], [], [attempt("completed", "failure")], BUILDER)
        self.assertEqual(plan["lock"]["version"], "3.6.2")

    def test_draft_and_moved_tag_are_not_overwritten(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        with self.assertRaisesRegex(ValueError, "draft"):
            updater.choose_build(lock, [tag()], [{"tag_name": lock["release_tag"], "draft": True}], [], BUILDER)
        with self.assertRaisesRegex(ValueError, "deleted or moved"):
            updater.choose_build(lock, [tag(sha=SHA)], [], [], BUILDER)

    def test_api_failure_is_not_treated_as_no_updates(self):
        with patch.object(updater.build, "run", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                updater.github("repos/example/repo/releases")

    def test_patch_failure_does_not_modify_lock_or_dispatch(self):
        plan = {"lock": updater.lock_for_tag(original_lock(), tag())}
        with patch.object(updater, "github", return_value={"default_branch": "main"}), \
             patch.object(updater.build, "run", side_effect=["", BUILDER + "\trefs/heads/main"]) as run, \
             patch.object(updater.build, "prepare", side_effect=ValueError("Patch does not apply")), \
             patch.object(updater.build, "write_json") as write:
            with self.assertRaisesRegex(ValueError, "Patch"):
                updater.execute("example/repo", original_lock(), BUILDER, plan)
            write.assert_not_called()
            self.assertEqual(run.call_count, 2)

    def test_resumed_dispatch_uses_immutable_builder_without_duplicate_commit(self):
        lock = updater.lock_for_tag(original_lock(), tag())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header = root / ".work/upstream" / NEW_SHA / "include/MNN/MNNDefine.h"
            header.parent.mkdir(parents=True)
            header.write_text("#define MNN_VERSION_MAJOR 3\n#define MNN_VERSION_MINOR 6\n#define MNN_VERSION_PATCH 1\n")
            with patch.object(updater, "ROOT", root), \
                 patch.object(updater, "github", return_value={"default_branch": "main"}), \
                 patch.object(updater.build, "prepare"), \
                 patch.object(updater.build, "run", side_effect=["", BUILDER + "\trefs/heads/main", None]) as run:
                self.assertEqual(updater.execute("example/repo", lock, BUILDER, {"lock": lock}), BUILDER)
                self.assertEqual(run.call_args.args[0], ["gh", "workflow", "run", "build.yml", "--repo", "example/repo",
                                                        "--ref", "main", "-f", "target=all", "-F", "publish=true",
                                                        "-f", "builder_ref=" + BUILDER])


if __name__ == "__main__":
    unittest.main()
