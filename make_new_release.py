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


def ask(prompt):
    """Prompt the user for yes/no confirmation."""
    try:
        return input(prompt).strip().lower() == "y"
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)


def get_current_version():
    """Gets the latest version tag from git."""
    try:
        tag = run(["git", "describe", "--tags", "--abbrev=0"], capture=True)
        return tag.lstrip("v.")
    except subprocess.CalledProcessError:
        return "0.0.0"


def get_bumped_version():
    """Gets the suggested next version from git-cliff."""
    try:
        bumped = run(["git", "cliff", "--bumped-version"], capture=True)
        ver = bumped.lstrip("v")
        # Enforce 0.X.Y space if git-cliff suggests 1.0.0+
        if ver.startswith("1."):
            current = get_current_version()
            _major, minor, _micro = map(int, current.split("."))
            return f"0.{minor + 1}.0"
        return ver
    except subprocess.CalledProcessError:
        return None


def bump_version(current, part):
    """Calculates the next version according to 0.X.Y rules."""
    parts = list(map(int, current.split(".")))
    while len(parts) < 3:
        parts.append(0)

    _major, minor, micro = parts
    if part == "minor":
        minor += 1
        micro = 0
    elif part == "micro":
        micro += 1

    return f"0.{minor}.{micro}"


def main():
    # 1. Must be on main
    branch = run(["git", "branch", "--show-current"], capture=True)
    if branch != "main":
        print(f"Error: Not on branch 'main' (currently on {branch!r}).")
        sys.exit(1)

    # 2. Working copy must be clean
    if run(["git", "diff", "--quiet"]) != 0:
        print("Error: Uncommitted changes in the repository.")
        print("Please commit or stash them before releasing.")
        sys.exit(1)

    if run(["git", "diff", "--cached", "--quiet"]) != 0:
        print("Error: Staged but uncommitted changes in the repository.")
        print("Please commit or stash them before releasing.")
        sys.exit(1)

    # 3. Lint first — fast feedback before anything else
    print("--- Running lint checks ---")
    if run(["tox", "-e", "lint"]) != 0:
        print("Error: Lint checks failed. Fix issues before releasing.")
        sys.exit(1)

    # 4. Push current main so CI picks it up
    if not ask("\nPush current main to origin? [y/N]: "):
        print("Aborted — main must be pushed before running full tests.")
        sys.exit(0)

    if run(["git", "push", "origin", "main"]) != 0:
        print("Error: git push failed.")
        sys.exit(1)

    # 5. Full test suite
    print("\n--- Running tests with tox ---")
    if run(["tox"]) != 0:
        print("Error: Tox tests failed. Aborting release.")
        sys.exit(1)

    print("--- Running functional tests (fullrun.py) ---")
    if run(["pytest", "tests/fullrun.py", "-v", "-x", "-s"]) != 0:
        print("Error: Functional tests (fullrun.py) failed. Aborting release.")
        sys.exit(1)

    # 6. Version selection
    current = get_current_version()
    print(f"\nCurrent version: v{current}")

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

    # 7. Preview and confirm
    print(f"\n--- Previewing unreleased changes for {tag} ---")
    run(["git", "cliff", "--unreleased"])

    if not ask(f"\nProceed with release {tag}? [y/N]: "):
        print("Cancelled.")
        return

    # 8. Generate changelog
    print(f"Generating CHANGELOG.md for {tag} ...")
    if run(["git", "cliff", "--tag", tag, "-o", "CHANGELOG.md"]) != 0:
        print("Error: Failed to generate changelog.")
        sys.exit(1)

    # 9. Allow manual edits to CHANGELOG.md
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    print(f"Opening CHANGELOG.md in {editor} for manual edits...")
    if run([editor, "CHANGELOG.md"]) != 0:
        print("Warning: Editor exited with error, continuing anyway.")

    # 10. Create release commit and tag
    print(f"Creating release commit for {tag} ...")
    run(["git", "add", "CHANGELOG.md"])
    if run(["git", "commit", "-m", f"chore: release {tag}"]) != 0:
        print("Error: Failed to create release commit.")
        sys.exit(1)

    print(f"Tagging {tag} ...")
    if run(["git", "tag", "-af", tag, "-m", f"Release {tag}"]) != 0:
        print("Error: Failed to create tag.")
        sys.exit(1)

    # 11. Final push with tag
    push_cmd = f"git push origin main {tag}"
    if not ask(f"\nPush release? Will run: {push_cmd}\n[y/N]: "):
        print(f"\nRelease {tag} created locally but NOT pushed.")
        print(f"When ready, run:\n  {push_cmd}")
        return

    if run(["git", "push", "origin", "main", tag]) != 0:
        print("Error: Push failed. Release commit and tag exist locally.")
        print(f"Retry manually:\n  {push_cmd}")
        sys.exit(1)

    print(f"\nSuccessfully released and pushed {tag}.")


if __name__ == "__main__":
    main()
