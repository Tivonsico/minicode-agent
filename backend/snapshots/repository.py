"""Independent Git object store. Each retained state is a parentless commit.

No worktree checkout, user index, hooks, filters, or normal Git refs are used.
The JSON catalog is written after refs, and refs are removed only after catalog
replacement, so a crash can leak objects but cannot prune a catalogued state.
"""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from backend.conversation.metadata import now
from backend.permissions.paths import PathPolicy, check_content
from backend.storage.files import atomic_json, internal_directory
from backend.tools.base import ToolError

from .locking import locked, repository_lock
from .paths import read_regular

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 50000


class ProjectSnapshots:
    def __init__(self, app_root, project, *, keep=20):
        self.app_root = Path(app_root).resolve()
        self.project = Path(project).resolve()
        self.project_id = hashlib.sha256(os.fsencode(self.project)).hexdigest()
        self.directory = internal_directory(self.app_root, "snapshots", self.project_id)
        self.repo = self.directory / "objects.git"
        self.catalog = self.directory / "rounds.json"
        self.keep = keep
        self.policy = PathPolicy(self.app_root, extra_roots=lambda: (self.project,),
                                 include_root=False)
        if self.repo.is_symlink() or self.catalog.is_symlink():
            raise OSError("Snapshot storage must not be a link")
        with repository_lock(self.directory):
            if not self.repo.exists():
                self._git("init", "--bare", "--quiet", str(self.repo), init=True)
            if not self.catalog.exists():
                atomic_json(self.catalog, [])

    def _git(self, *args, data=None, init=False, index=None, accepted=(0,)):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="MiniCode",
                   GIT_AUTHOR_EMAIL="snapshot@localhost", GIT_COMMITTER_NAME="MiniCode",
                   GIT_COMMITTER_EMAIL="snapshot@localhost")
        if index:
            env["GIT_INDEX_FILE"] = str(index)
        command = ["git", "--no-replace-objects", "-c", "core.fsync=all",
                   "-c", "core.hooksPath=" + os.devnull]
        if not init:
            command += ["--git-dir=" + str(self.repo)]
        try:
            result = subprocess.run(command + list(args), input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError("文件快照需要本地 Git，保存失败；任务未获快照保护。") from exc
        if result.returncode not in accepted:
            raise OSError("文件快照仓库操作失败，请检查磁盘空间和本地 Git。")
        return result.stdout

    def _records(self):
        return json.loads(self.catalog.read_text(encoding="utf-8"))

    @locked
    def listing(self, session_id=None):
        return [r for r in reversed(self._records())
                if session_id is None or r["session_id"] == session_id]

    @locked
    def record(self, round_id):
        for record in self._records():
            if record["id"] == round_id:
                return record
        raise ValueError("该轮文件快照已清理或不存在")

    def _save(self, record):
        records = self._records()
        for i, item in enumerate(records):
            if item["id"] == record["id"]:
                records[i] = record
                break
        else:
            records.append(record)
        atomic_json(self.catalog, records)

    @locked
    def capture(self):
        # An empty, private index enumerates untracked files with the project's
        # .gitignore rules, including in projects with no ordinary Git repository.
        names = self._git("--work-tree=" + str(self.project), "ls-files", "--others",
                          "--exclude-standard", "--exclude=.git/", "--exclude=.minicode/",
                          "--exclude=PROJECT_SKILLS/", "-z").split(b"\0")
        entries, skipped, total = [], [], 0
        if len(names) > MAX_FILES + 1:
            raise OSError("项目文件超过快照上限，请通过 .gitignore 缩小范围后重试。")
        for raw in names:
            if not raw:
                continue
            name = os.fsdecode(raw)
            path = self.project / name
            # Never follow symlinks, including directory aliases inside the project.
            if any(p.is_symlink() for p in [path, *path.parents]
                   if p.is_relative_to(self.project)):
                skipped.append({"path": name, "reason": "符号链接不支持"})
                continue
            try:
                self.policy.resolve(str(path))
            except ToolError:
                # Do not persist names/content of sensitive or internal files.
                continue
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                skipped.append({"path": name, "reason": "仅支持普通文件"})
                continue
            if before.st_size > MAX_FILE_BYTES:
                skipped.append({"path": name, "reason": "文件超过 8 MiB"})
                continue
            data, observed = read_regular(self.project, path, MAX_FILE_BYTES)
            if (observed.st_dev, observed.st_ino, observed.st_mtime_ns, observed.st_size,
                observed.st_mode) != (before.st_dev, before.st_ino, before.st_mtime_ns,
                                     before.st_size, before.st_mode) or len(data) > MAX_FILE_BYTES:
                raise OSError("保存快照时文件发生变化，请等待外部写入结束后重试。")
            # A no-follow file open does not prevent a parent directory from
            # becoming a symlink after the first policy check. Revalidate before
            # hashing any bytes into the protected object store.
            self.policy.resolve(str(path))
            if any(p.is_symlink() for p in [path, *path.parents]
                   if p.is_relative_to(self.project)):
                raise OSError("保存快照时文件路径发生变化，请停止外部写入后重试。")
            try:
                check_content(data.decode("utf-8", errors="replace"))
            except ToolError:
                skipped.append({"path": name, "reason": "敏感内容不保存"})
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise OSError("项目快照超过 256 MiB，请通过 .gitignore 缩小范围后重试。")
            oid = self._git("hash-object", "-w", "--stdin", data=data).strip()
            mode = b"100755" if before.st_mode & stat.S_IXUSR else b"100644"
            entries.append(mode + b" " + oid + b"\t" + raw + b"\0")
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix="index-")
        os.close(fd)
        os.unlink(temporary)
        try:
            self._git("read-tree", "--empty", index=temporary)
            self._git("update-index", "-z", "--index-info", data=b"".join(entries), index=temporary)
            tree = self._git("write-tree", index=temporary).strip().decode("ascii")
        finally:
            Path(temporary).unlink(missing_ok=True)
            Path(temporary + ".lock").unlink(missing_ok=True)
        commit = self._git("commit-tree", tree, data=b"MiniCode file state\n").strip().decode()
        return commit, skipped

    @locked
    def begin(self, session_id, run_id, turn_id, *, kind="task"):
        commit, skipped = self.capture()
        record = {"id": uuid.uuid4().hex, "project_id": self.project_id,
                  "project": str(self.project), "session_id": session_id, "run_id": run_id,
                  "turn_id": turn_id, "kind": kind, "created_at": now(), "status": "running",
                  "before": commit, "after": None, "skipped_before": skipped, "pinned": False}
        self._git("update-ref", f"refs/rounds/{record['id']}/before", commit)
        self._save(record)
        return record

    @locked
    def finish(self, round_id, status, *, pinned=False):
        record = self.record(round_id)
        commit, skipped = self.capture()
        self._git("update-ref", f"refs/rounds/{round_id}/after", commit)
        skipped.extend({"path": name, "reason": "当前被 .gitignore 排除"}
                       for name in self.ignored(self.files(record["before"])))
        record.update(after=commit, status=status, ended_at=now(), skipped_after=skipped,
                      pinned=pinned)
        self._save(record)
        self.prune()
        return record

    @locked
    def prune(self):
        records = self._records()
        complete = [r for r in records if r["after"] and not r["pinned"]
                    and r["status"] not in {"running", "unknown"}]
        expired = {r["id"] for r in complete[:-self.keep]}
        retained = [r for r in records if r["id"] not in expired]
        atomic_json(self.catalog, retained)
        wanted = {f"refs/rounds/{r['id']}/{phase}" for r in retained
                  for phase in ("before", "after") if r.get(phase)}
        refs = self._git("for-each-ref", "--format=%(refname)", "refs/rounds/").decode().splitlines()
        for ref in refs:
            if ref not in wanted:
                self._git("update-ref", "-d", ref)
        if expired or set(refs) - wanted:
            self._git("reflog", "expire", "--expire=now", "--all")
            self._git("gc", "--prune=now", "--quiet")

    def files(self, commit):
        result = {}
        for entry in self._git("ls-tree", "-rz", commit).split(b"\0"):
            if entry:
                info, name = entry.split(b"\t", 1)
                mode, _, oid = info.split()
                result[os.fsdecode(name)] = {"mode": mode.decode(), "oid": oid.decode()}
        return result

    def ignored(self, names):
        raw = b"".join(os.fsencode(name) + b"\0" for name in names)
        if not raw:
            return []
        return [os.fsdecode(name) for name in self._git(
            "--work-tree=" + str(self.project), "check-ignore", "--no-index", "-z", "--stdin",
            data=raw, accepted=(0, 1)).split(b"\0") if name]

    def content(self, entry):
        return self._git("cat-file", "blob", entry["oid"]) if entry else b""

    @locked
    def changes(self, round_id):
        from .diff import compare
        record = self.record(round_id)
        if not record["after"]:
            return {"files": [], "added": 0, "deleted": 0, "pending": True}
        return compare(self, record["before"], record["after"],
                       record["skipped_before"] + record.get("skipped_after", []))

    @locked
    def preview_restore(self, round_id):
        from .restore import preview
        return preview(self, round_id)

    @locked
    def restore(self, round_id, token, *, accept_conflicts=False, before_effect=None):
        from .restore import restore
        return restore(self, round_id, token, accept_conflicts=accept_conflicts,
                       before_effect=before_effect)

    @locked
    def pin(self, round_id, pinned):
        record = self.record(round_id)
        record["pinned"] = pinned
        self._save(record)
        self.prune()
