#!/usr/bin/env python3
"""
Automate the release process for cmlxc.
"""

import os
import subprocess
import sys


def run(cmd, capture=False):
    """Run a command and return output or exit code."""
    if capture:
        return subprocess.check_output(cmd, text=True).strip()
    return subprocess.run(cmd).returncode


def get_current_version():
    """Get the latest version tag from git."""
    try:
        tag = run(["git", "describe", "--tags", "--abbrev=0"], capture=True)
        return tag.lstrip("v.")
    except subprocess.CalledProcessError:
        return "0.0.0"


def get_bumped_version():
    """Get the suggested next version from git-cliff."""
    try:
        bumped = run(["git", "cliff", "--bumped-version"], capture=True)
        ver = bumped.lstrip("v")
        # Enforce 0.X.Y space if git-cliff suggests 1.0.0+
        if ver.startswith("1."):
            current = get_current_version()
            major, minor, micro = map(int, current.split("."))
            return f"0.{minor + 1}.0"
        return ver
    except subprocess.CalledProcessError:
        return None


def bump_version(current, part):
    """Calculate the next version according to 0.X.Y rules."""
    parts = list(map(int, current.split(".")))
    while len(parts) < 3:
        parts.append(0)

    major, minor, micro = parts
    if part == "minor":
        minor += 1
        micro = 0
    elif part == "micro":
        micro += 1

    return f"0.{minor}.{micro}"


def main():
    if run(["git", "diff", "--quiet"]) != 0:
        print("Error: Uncommitted changes in the repository.")
        print("Please commit or stash them before releasing.")
        sys.exit(1)

    print("--- Running tests with tox ---")
    if run(["tox"]) != 0:
        print("Error: Tox tests failed. Aborting release.")
        sys.exit(1)

    print("--- Running functional tests (fullrun.py) ---")
    if run(["pytest", "tests/fullrun.py", "-v", "-x", "-s"]) != 0:
        print("Error: Functional tests (fullrun.py) failed. Aborting release.")
        sys.exit(1)

    current = get_current_version()
    print(f"Current version: v{current}")

    # Auto-suggestion from git-cliff
    auto_next = get_bumped_version()
    minor_next = bump_version(current, "minor")
    micro_next = bump_version(current, "micro")

    print("\nSuggested next versions:")
    if auto_next:
        print(f"  0. Auto-detected:    v{auto_next} (from git-cliff)")
    print(f"  1. Minor (CLI-changes): v{minor_next}")
    print(f"  2. Micro (Non-breaking): v{micro_next}")

    default = "0" if auto_next else "2"
    prompt = f"\nSelect version (default {default}) or enter custom: "

    try:
        choice = input(prompt).strip()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    if not choice:
        choice = default

    if choice == "0" and auto_next:
        next_ver = auto_next
    elif choice == "1":
        next_ver = minor_next
    elif choice == "2":
        next_ver = micro_next
    else:
        next_ver = choice.lstrip("v")

    tag = f"v{next_ver}"

    print(f"\n--- Previewing unreleased changes for {tag} ---")
    run(["git", "cliff", "--unreleased"])

    try:
        confirm = input(f"\nProceed with release {tag}? [y/N]: ").lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    if confirm != "y":
        print("Cancelled.")
        return

    # 1. Tag
    print(f"Tagging {tag} ...")
    if run(["git", "tag", tag]) != 0:
        print("Error: Failed to create tag.")
        sys.exit(1)

    # 2. Generate changelog
    print("Generating CHANGELOG.md ...")
    if run(["git", "cliff", "-o", "CHANGELOG.md"]) != 0:
        print("Error: Failed to generate changelog.")
        # Cleanup tag
        run(["git", "tag", "-d", tag], capture=True)
        sys.exit(1)

    # 3. Allow manual edits to CHANGELOG.md
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    print(f"Opening CHANGELOG.md in {editor} for manual edits...")
    if run([editor, "CHANGELOG.md"]) != 0:
        print("Warning: Editor exited with error, continuing anyway.")

    # 4. Amend the tag commit to include CHANGELOG.md
    print("Amending commit with changelog ...")
    run(["git", "add", "CHANGELOG.md"])
    if run(["git", "commit", "--amend", "--no-edit"]) != 0:
        print("Error: Failed to amend commit.")
        sys.exit(1)

    # 5. Force-tag the amended commit
    print(f"Updating tag {tag} to amended commit ...")
    if run(["git", "tag", "-f", tag]) != 0:
        print("Error: Failed to update tag.")
        sys.exit(1)

    print(f"\nSuccessfully released {tag}.")
    print("\nNext steps:")
    print("  git push origin main --tags")


if __name__ == "__main__":
    main()
