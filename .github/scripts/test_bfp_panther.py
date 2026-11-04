import json
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
            "stable_commit": "1a8763b93150b2c8f3992c27a5ce7857fee4ab0f",
        }
        self.assertNotEqual(dispatch["root_flavor"].lower(), "all")
        config = json.loads((ROOT / ".github/config/android14-6.1.json").read_text(encoding="utf-8"))
        selected = [row for row in config["include"] if row.get("disabled") is not True and row["date"] == dispatch["os_patch_level"]]
        self.assertEqual(1, len(selected))


if __name__ == "__main__":
    unittest.main()
