import fnmatch
import time
from functools import lru_cache

from .base import ToolError

DEPENDENCY_DIRS = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox",
})


class SearchOperations:
    @staticmethod
    def _matches(relative, pattern):
        path_parts = relative.replace("\\", "/").split("/")
        pattern_parts = pattern.replace("\\", "/").split("/")

        @lru_cache(maxsize=None)
        def match(path_index, pattern_index):
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            part = pattern_parts[pattern_index]
            if part == "**":
                return match(path_index, pattern_index + 1) or (
                    path_index < len(path_parts) and match(path_index + 1, pattern_index)
                )
            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], part)
                and match(path_index + 1, pattern_index + 1)
            )

        return match(0, 0)

    def _search_files(self, name, glob_pattern, needle, stop):
        segments = glob_pattern.replace("\\", "/").split("/")
        max_depth = None if "**" in segments else len(segments)
        deadline = time.monotonic() + 7
        matches = []
        try:
            for path in self.policy.files(
                max_depth=max_depth,
                include_dirs=name == "Glob",
                stop=stop,
                deadline=deadline,
                max_entries=10000,
                skip_dirs=DEPENDENCY_DIRS.difference(segments),
            ):
                relative = self.policy.match_name(path)
                if not self._matches(relative, glob_pattern):
                    continue
                display = self.policy.display(path)
                if name == "Glob":
                    matches.append(display + ("/" if path.is_dir() else ""))
                else:
                    try:
                        if path.stat().st_size > 2_000_000:
                            continue
                        self.policy.resolve(str(path))
                        found = self._grep_matches(path, needle, display)
                        if self.policy.resolve(str(path)) != path:
                            continue
                    except (ToolError, OSError, UnicodeError):
                        continue
                    matches.extend(found)
                if len(matches) > 500:
                    return "\n".join(matches[:500]) + "\n[Search truncated at 500 results; use a narrower pattern.]"
        except ToolError as error:
            if error.error_type != "search_limit":
                raise
            return "\n".join(matches) + "\n[Search truncated at the scan/time limit; results are incomplete. Use a narrower pattern.]"
        return "\n".join(matches) or "No matches."
