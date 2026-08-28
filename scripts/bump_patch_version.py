# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Pre-commit hook: bump the patch version on every commit.

A patch bump is not a release (see RELEASING.md) — it is just a running
counter of "something changed". Minor/major bumps remain a deliberate,
manual chore(release) commit, which is why this hook backs off whenever
netbox_opennms/__init__.py is already staged: that means a release bump is
already in progress and must not be clobbered.
"""

import re
import subprocess
import sys
from pathlib import Path

INIT_PATH = Path("netbox_opennms/__init__.py")
README_PATH = Path("README.md")
VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
README_PIN_RE = re.compile(r"netbox-opennms-plugin==\d+\.\d+\.\d+")


def staged_files() -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.splitlines())


def main() -> int:
    if str(INIT_PATH) in staged_files():
        return 0

    text = INIT_PATH.read_text()
    match = VERSION_RE.search(text)
    if not match:
        print(
            f"bump_patch_version: no __version__ found in {INIT_PATH}", file=sys.stderr
        )
        return 1

    major, minor, patch = (int(part) for part in match.groups())
    new_version = f"{major}.{minor}.{patch + 1}"
    INIT_PATH.write_text(
        text[: match.start()] + f'__version__ = "{new_version}"' + text[match.end() :]
    )

    readme = README_PATH.read_text()
    README_PATH.write_text(
        README_PIN_RE.sub(f"netbox-opennms-plugin=={new_version}", readme)
    )

    subprocess.run(["git", "add", str(INIT_PATH), str(README_PATH)], check=True)
    print(f"bump_patch_version: {major}.{minor}.{patch} -> {new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
