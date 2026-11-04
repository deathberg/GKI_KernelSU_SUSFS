import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    with (ROOT / ".github/workflows" / name).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if True in document:
        document["on"] = document.pop(True)
    return document


def action(name):
    with (ROOT / ".github/actions" / name / "action.yml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class StableInputTests(unittest.TestCase):
    def test_stable_commit_mapping(self):
        main = workflow("main.yml")["on"]["workflow_dispatch"]["inputs"]
        prepare = workflow("prepare.yml")["on"]["workflow_call"]["inputs"]
        build = workflow("build.yml")["on"]["workflow_call"]["inputs"]
        for inputs in (main, prepare, build):
            self.assertIn("stable_commit", inputs)
            self.assertEqual("", inputs["stable_commit"]["default"])
            self.assertFalse(inputs["stable_commit"]["required"])
        self.assertNotIn("use_cache", main)
        for name in ("main.yml", "prepare.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("stable_commit: ${{ inputs.stable_commit }}", text)

    def test_base_ref_mapping(self):
        inputs = action("upgrade-stable")["inputs"]
        self.assertIn("base_ref", inputs)
        self.assertEqual("", inputs["base_ref"]["default"])
        self.assertFalse(inputs["base_ref"]["required"])
        text = (ROOT / ".github/actions/upgrade-stable/action.yml").read_text(encoding="utf-8")
        self.assertIn("BASE_REF: ${{ inputs.base_ref }}", text)
        build = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn("base_ref: ${{ inputs.version }}-${{ inputs.os_patch_level }}", build)

    def test_localversion_suffix_input(self):
        inputs = action("apply-kernel-branding")["inputs"]
        self.assertIn("localversion_suffix", inputs)
        self.assertEqual("", inputs["localversion_suffix"]["default"])
        self.assertFalse(inputs["localversion_suffix"]["required"])

    def test_dispatch_selects_one_root_and_one_lts_row(self):
        dispatch = {
            "root_flavor": "KernelSU-Next",
            "kernel_build_version": "6.1.x-android14",
            "os_patch_level": "lts",
            "stable_commit": "",
        }
        self.assertNotEqual(dispatch["root_flavor"].lower(), "all")
        config = json.loads((ROOT / ".github/config/android14-6.1.json").read_text(encoding="utf-8"))
        selected = [row for row in config["include"] if row.get("disabled") is not True and row["date"] == dispatch["os_patch_level"]]
        self.assertEqual(1, len(selected))


class StableUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base_repo = self.root / "base-remote"
        self.stable_repo = self.root / "stable-remote"
        self.clone = self.root / "kernel/common"
        self.clone.parent.mkdir(parents=True)
        self.init_repo(self.base_repo, 182)
        self.git("clone", "-q", str(self.base_repo), str(self.stable_repo))
        (self.stable_repo / "Makefile").write_text("VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 188\n", encoding="utf-8")
        self.git("config", "user.name", "Kimi Code", cwd=self.stable_repo)
        self.git("config", "user.email", "kimi-code@localhost", cwd=self.stable_repo)
        self.git("add", "Makefile", cwd=self.stable_repo)
        self.git("commit", "-qm", "stable 188", cwd=self.stable_repo)
        self.stable_sha = self.git("rev-parse", "HEAD", cwd=self.stable_repo).stdout.strip()
        self.git("clone", "-q", "--depth", "1", "file://" + str(self.base_repo), str(self.clone))
        self.git("config", "user.name", "Kimi Code", cwd=self.clone)
        self.git("config", "user.email", "kimi-code@localhost", cwd=self.clone)
        self.base_sha = self.git("rev-parse", "HEAD", cwd=self.clone).stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=ROOT):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def init_repo(self, path, sublevel):
        path.mkdir(parents=True)
        self.git("init", "-q", cwd=path)
        (path / "Makefile").write_text(f"VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = {sublevel}\n", encoding="utf-8")
        self.git("config", "user.name", "Kimi Code", cwd=path)
        self.git("config", "user.email", "kimi-code@localhost", cwd=path)
        self.git("add", "Makefile", cwd=path)
        self.git("commit", "-qm", f"gki {sublevel}", cwd=path)

    def run_upgrade(self, commit, base_repository=None, extra_env=None):
        env = os.environ.copy()
        env.update({
            "GITHUB_WORKSPACE": str(self.root),
            "GITHUB_ENV": str(self.root / "github-env"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "STABLE_COMMIT": commit,
            "STABLE_REPOSITORY": str(self.stable_repo),
            "BASE_REPOSITORY": str(self.base_repo) if base_repository is None else base_repository,
            "REQUIRED_SUBLEVEL": "188",
        })
        env.update(extra_env or {})
        script = ROOT / ".github/actions/upgrade-stable/run.sh"
        self.assertTrue(script.is_file(), script)
        return subprocess.run(["bash", str(script)], cwd=self.root, env=env,
                              capture_output=True, text=True)

    def test_transient_fetch_failure_is_retried(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        attempts = self.root / "fetch-attempts"
        wrapper = fake_bin / "git"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$3\" = fetch ] && [[ \"$*\" == *\"$TRANSIENT_REPOSITORY\"* ]]; then\n"
            "  count=0\n"
            "  if [ -f \"$TRANSIENT_ATTEMPTS\" ]; then count=$(<\"$TRANSIENT_ATTEMPTS\"); fi\n"
            "  count=$((count+1))\n"
            "  printf '%s\\n' \"$count\" > \"$TRANSIENT_ATTEMPTS\"\n"
            "  if [ \"$count\" -lt 3 ]; then echo 'simulated transient fetch failure' >&2; exit 128; fi\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        result = self.run_upgrade(self.stable_sha, extra_env={
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "REAL_GIT": shutil.which("git"),
            "TRANSIENT_REPOSITORY": str(self.stable_repo),
            "TRANSIENT_ATTEMPTS": str(attempts),
            "FETCH_RETRY_DELAY_SECONDS": "0",
        })
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("3", attempts.read_text().strip())
        parents = self.git("show", "-s", "--format=%P", "HEAD", cwd=self.clone).stdout.split()
        self.assertEqual(2, len(parents))

    def test_bounded_depth_and_base_ref_fetch(self):
        base_ref = self.git("symbolic-ref", "--short", "HEAD", cwd=self.base_repo).stdout.strip()
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        calls = self.root / "fetch-calls"
        wrapper = fake_bin / "git"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$3\" = fetch ]; then\n"
            "  if [[ \"$*\" == *--deepen* ]]; then echo 'unsupported deepen operation' >&2; exit 99; fi\n"
            "  printf '%s\\n' \"$*\" >> \"$FETCH_CALLS\"\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        result = self.run_upgrade(self.stable_sha, extra_env={
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "REAL_GIT": shutil.which("git"),
            "BASE_REF": base_ref,
            "FETCH_CALLS": str(calls),
            "FETCH_RETRY_DELAY_SECONDS": "0",
        })
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        text = calls.read_text()
        self.assertIn(f"--depth=256 {self.base_repo} {base_ref}", text)
        self.assertIn(f"--depth=2560 {self.stable_repo} {self.stable_sha}", text)
        self.assertNotIn("--deepen", text)
        parents = self.git("show", "-s", "--format=%P", "HEAD", cwd=self.clone).stdout.split()
        self.assertEqual(2, len(parents))

    def test_upgrade_merges_stable_and_records_provenance(self):
        result = self.run_upgrade(self.stable_sha)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        merged = self.git("rev-parse", "HEAD", cwd=self.clone).stdout.strip()
        parents = self.git("show", "-s", "--format=%P", "HEAD", cwd=self.clone).stdout.split()
        self.assertEqual(2, len(parents))
        self.assertIn(f"KERNEL_SOURCE_COMMIT={merged}", (self.root / "github-env").read_text())
        manifest = json.loads((self.root / "kernel/stable-provenance/manifest.json").read_text())
        self.assertEqual(self.base_sha, manifest["gki_base_commit"])
        self.assertEqual(self.stable_sha, manifest["stable_commit"])
        self.assertEqual(merged, manifest["merged_head"])
        self.assertEqual(str(self.stable_repo), manifest["stable_repository"])
        self.assertGreater((self.root / "kernel/stable-provenance/stable-upgrade.patch").stat().st_size, 0)
        self.assertIn("SUBLEVEL = 188", (self.clone / "Makefile").read_text())
        build = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertLess(build.index("actions/download-kernel"), build.index("actions/upgrade-stable"))
        self.assertLess(build.index("actions/upgrade-stable"), build.index("actions/stock-config"))

    def test_repo_aosp_remote(self):
        self.git("remote", "rename", "origin", "aosp", cwd=self.clone)
        result = self.run_upgrade(self.stable_sha, base_repository="")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("SUBLEVEL = 188", (self.clone / "Makefile").read_text())

    def test_empty_stable_commit_is_noop(self):
        result = self.run_upgrade("")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(self.base_sha, self.git("rev-parse", "HEAD", cwd=self.clone).stdout.strip())
        self.assertFalse((self.root / "kernel/stable-provenance/manifest.json").exists())


class EvidenceTests(unittest.TestCase):
    def run_branding(self, suffix):
        with tempfile.TemporaryDirectory() as temp:
            scripts = Path(temp) / "scripts"
            scripts.mkdir()
            (scripts / "setlocalversion").write_text("#!/bin/sh\necho '-original'\n", encoding="utf-8")
            text = action("apply-kernel-branding")["runs"]["steps"][0]["run"]
            replacements = {
                "${{ inputs.version }}": "android14-6.1",
                "${{ inputs.sublevel }}": "188",
                "${{ inputs.brand_name }}": "BFP",
                "${{ inputs.keep_kmi }}": "false",
                "${{ inputs.localversion_suffix }}": suffix,
            }
            for token, value in replacements.items():
                text = text.replace(token, value)
            result = subprocess.run(["bash", "-c", text], cwd=temp,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return (scripts / "setlocalversion").read_text()

    def test_exact_localversion_suffix(self):
        text = self.run_branding("Budapest")
        self.assertIn('echo "-Budapest"', text)
        self.assertNotIn('echo "-android14-BFP"', text)

    def test_empty_localversion_suffix_preserves_legacy_branding(self):
        text = self.run_branding("")
        self.assertIn('echo "-android14-BFP"', text)
        self.assertNotIn('echo "-Budapest"', text)

    def test_stock_config_disabled_and_numeric_summary(self):
        document = workflow("build.yml")
        steps = document["jobs"]["build-gki"]["steps"]
        stock = next(step for step in steps if step.get("uses") == "./.github/actions/stock-config")
        self.assertIs(stock.get("if"), False)
        summary = next(step for step in steps if step.get("name") == "Consolidated Build Summary")
        self.assertEqual("${{ steps.extract.outputs.sublevel }}", summary["with"]["sublevel"])

    def test_required_config_and_exact_image_version(self):
        inputs = action("build-kernel")["inputs"]
        self.assertIn("require_verified_config", inputs)
        self.assertFalse(inputs["require_verified_config"]["default"])
        text = (ROOT / ".github/actions/build-kernel/action.yml").read_text(encoding="utf-8")
        for required in ("CONFIG_ARM64_4K_PAGES=y", "CONFIG_MODVERSIONS=y",
                         "CONFIG_NOMOUNT=y", "CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y"):
            self.assertIn(required, text)
        self.assertIn("require_verified_config", text)
        build = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn("6.1.182-Budapest", build)
        self.assertIn("strings", build)
        document = workflow("build.yml")
        step = next(step for step in document["jobs"]["build-gki"]["steps"]
                    if step.get("uses") == "./.github/actions/build-kernel")
        self.assertEqual("${{ inputs.root_flavor != 'none' }}", step["with"]["require_verified_config"])

    def test_provenance_upload_includes_hidden_config(self):
        steps = workflow("build.yml")["jobs"]["build-gki"]["steps"]
        upload = next(step for step in steps if step.get("name") == "Upload BFP provenance")
        self.assertIs(upload["with"].get("include-hidden-files"), True)

    def test_supported_gki_baseline_provenance(self):
        document = workflow("build.yml")
        steps = document["jobs"]["build-gki"]["steps"]
        baseline = next(step for step in steps if step.get("name") == "Record GKI baseline provenance")
        self.assertEqual("inputs.stable_commit == ''", baseline["if"])
        text = baseline["run"]
        for expected in ("fcab623b3d2b0107e56b44e4e791f9a1cc5ad0ff",
                         "BASE_REF", "source_mode", "baseline",
                         "stable-upgrade.patch"):
            self.assertIn(expected, text)
        self.assertIn(": > kernel/stable-provenance/stable-upgrade.patch", text)
        self.assertLess(steps.index(baseline), steps.index(next(
            step for step in steps if step.get("uses") == "./.github/actions/upgrade-stable")))
        verify = next(step for step in steps if step.get("name") == "Verify image and collect BFP provenance")["run"]
        self.assertIn('EXPECTED_RELEASE="6.1.182-Budapest"', verify)
        self.assertIn("EXPECTED_SUBLEVEL=182", verify)
        self.assertIn('if [ -n "${{ inputs.stable_commit }}" ]', verify)
        self.assertIn("test -f kernel/stable-provenance/stable-upgrade.patch", verify)
        upgrade = next(step for step in steps if step.get("uses") == "./.github/actions/upgrade-stable")
        self.assertIn("${{ inputs.stable_commit == '' && '182' || '188' }}", upgrade["with"]["required_sublevel"])

    def test_provenance_artifacts_and_validation(self):
        build = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn("bfp-provenance", build)
        self.assertIn("Module.symvers", build)
        self.assertIn("KMI_GENERATION", build)
        self.assertIn("stable-provenance", build)
        self.assertIn("scripts/setlocalversion", build)
        validation = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn("unittest discover -s .github/scripts -p 'test_bfp_panther.py' -v", validation)


if __name__ == "__main__":
    unittest.main()
