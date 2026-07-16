from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DEFAULT_SCAN_ROOTS = (
    Path("src/kokoro_link/application"),
    Path("src/kokoro_link/domain"),
)
DEFAULT_ALLOWLIST = Path("scripts/clock_guard_allowlist.txt")
DIRECT_NOW_PATTERN = re.compile(r"\bdatetime\.now\s*\(")


def load_allowlist(project_root: Path, allowlist_path: Path = DEFAULT_ALLOWLIST) -> set[str]:
    path = _resolve(project_root, allowlist_path)
    if not path.exists():
        return set()
    return {
        line.strip().replace("\\", "/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def find_violations(
    paths: list[Path] | None = None,
    *,
    project_root: Path | None = None,
    allowlist_path: Path = DEFAULT_ALLOWLIST,
) -> list[str]:
    root = (project_root or Path.cwd()).resolve()
    allowlist = load_allowlist(root, allowlist_path)
    candidates = paths if paths else list(DEFAULT_SCAN_ROOTS)
    violations: list[str] = []
    for candidate in candidates:
        target = _resolve(root, candidate)
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = sorted(target.rglob("*.py"))
        else:
            continue
        for path in files:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if not DIRECT_NOW_PATTERN.search(text):
                continue
            rel = _relative_name(root, path)
            if rel not in allowlist:
                violations.append(rel)
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Block new direct datetime.now() usage in domain/application "
            "outside the ClockPort migration allowlist."
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="Allowlist path relative to project root.",
    )
    args = parser.parse_args(argv)
    violations = find_violations(args.paths or None, allowlist_path=args.allowlist)
    if not violations:
        return 0
    print("Direct datetime.now() is not allowed outside ClockPort allowlist:")
    for rel in violations:
        print(f"- {rel}")
    return 1


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _relative_name(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


if __name__ == "__main__":
    sys.exit(main())

