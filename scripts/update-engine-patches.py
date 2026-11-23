# SPDX-License-Identifier: Apache-2.0
"""After changing the engine (third_party/cycles): save the change as new files in patches/.

For the public engine route (bootstrap without the private authoring fork). Steps:
1. commits uncommitted engine edits (one commit, with the publisher name from
   build-identity.json and the address engine-edit@localhost, unless --my-git-identity);
2. exports every engine commit made after the locked engine (the commit whose Git tree is
   cycles.authoring_tree) as one new patch file each, numbered after the existing ones;
3. checks that upstream Cycles plus all patches gives exactly the engine's files again;
4. records the new patches, their SHA-256 and the new cycles.authoring_tree in
   source.lock.json, and (after asking, or with --yes) commits patches/ and source.lock.json.
Nothing is changed before the first "yes". Existing patch files are never changed.
cycles.authoring_tip (the private fork's commit) is left as it is. A fresh clone then
rebuilds the same engine (scripts/bootstrap.ps1).
Privacy: patch dates are written in UTC (no local time zone). An e-mail address other
than the tool's own placeholder is only written into a patch after a person answers "y";
--yes does not answer that question. The commit in this folder uses your own Git
identity when you have set one (as every Git commit does).
An interrupted run is continued by running the tool again.
The private authoring fork keeps its own tool, scripts/export-engine-patches.py.
"""

import argparse
import datetime
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ENGINE_EMAIL = "engine-edit@localhost"


class Stop(RuntimeError):
    pass


def git(repo, *args, env=None, check=True):
    done = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args], capture_output=True, env=env)
    if check and done.returncode:
        raise Stop(f"git {' '.join(args[:3])} failed: " + done.stderr.decode(errors="replace").strip())
    return done.stdout.decode(errors="replace").strip() if not check or done.returncode == 0 else ""


def ask(question, answer):
    if answer is not None:
        print(f"{question} [y/N] {'y' if answer else 'n'}")
        return answer
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
    except EOFError:
        return False


def read_lock(root):
    raw = (root / "source.lock.json").read_bytes()
    return json.loads(raw.decode("utf-8-sig")), b"\r\n" in raw


def write_lock(root, lock, crlf):
    text = json.dumps(lock, indent=2) + "\n"
    temporary = root / "source.lock.json.tmp"
    temporary.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    os.replace(temporary, root / "source.lock.json")


def utc_now():
    """A Git date for "now" in UTC, so no local time zone reaches a patch."""
    return f"{int(time.time())} +0000"


PATCH_DATE = re.compile(rb"^Date: (.+)$", re.MULTILINE)


def utc_patch_dates(data):
    """Rewrite the Date: line of a format-patch header to the same moment in UTC."""
    header, separator, body = data.partition(b"\n\n")

    def to_utc(match):
        moment = email.utils.parsedate_to_datetime(match.group(1).decode("ascii"))
        return b"Date: " + email.utils.format_datetime(moment.astimezone(datetime.timezone.utc)).encode("ascii")

    return PATCH_DATE.sub(to_utc, header, count=1) + separator + body


def next_patch_number(root, engine):
    names = list(engine["patches"])
    triage = root / "patches/triage.json"
    if triage.is_file():
        names.extend(row["path"] for row in json.loads(triage.read_text(encoding="utf-8")).get("retired_patches", []))
    return max((int(Path(name).name.split("-", 1)[0]) for name in names), default=0) + 1


def publisher(root):
    path = root / "build-identity.json"
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8-sig")).get("publisher")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Community builder"


def normalize_line_endings(engine, names):
    """Windows editors may save LF files with CRLF; patches/ must stay LF (see engine-fork.py)."""
    fixed = []
    for name in names:
        path = engine / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data or b"\r\n" not in data:
            continue
        old = subprocess.run(["git", "-C", str(engine), "show", "HEAD:" + name], capture_output=True)
        if old.returncode == 0 and b"\r" in old.stdout:
            continue  # the file had CRLF before; leave it (the check below stops if it matters)
        path.write_bytes(data.replace(b"\r\n", b"\n"))
        fixed.append(name)
    return fixed


PATCH_AUTHOR = re.compile(rb"^From: (.+?) <([^<>]*)>$", re.MULTILINE)


def canonical_commits(engine, base, patches):
    """Apply the new patches to base exactly as engine-fork.py materialize-public does.

    The public replay is deterministic (committer = patch author, committer date = author
    date), so this gives the commit ids a fresh clone will have. Must stay in step with
    materialize_public in engine-fork.py.
    """
    with tempfile.TemporaryDirectory(prefix="cycles-canonical-") as temporary:
        work = Path(temporary) / "engine"
        env = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            env.pop(name, None)
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(engine), str(work)], check=True, env=env)
        git(work, "checkout", "-q", "--detach", base, env=env)
        for data in patches:
            author = PATCH_AUTHOR.search(data.split(b"\n\n", 1)[0])
            if author is None:
                raise Stop("Exported patch has no From: author line")
            extra = dict(
                env,
                GIT_COMMITTER_NAME=author.group(1).decode("utf-8"),
                GIT_COMMITTER_EMAIL=author.group(2).decode("utf-8"),
            )
            done = subprocess.run(
                ["git", "-C", str(work), "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                 "-c", "apply.whitespace=nowarn", "am", "-q", "--no-3way", "--whitespace=nowarn",
                 "--committer-date-is-author-date"],
                input=data, env=extra, capture_output=True,
            )  # fmt: skip
            if done.returncode:
                raise Stop(
                    "A new patch does not apply like a fresh clone applies it: " + done.stderr.decode(errors="replace")
                )
        head = git(work, "rev-parse", "HEAD", env=env)
        git(engine, "fetch", "-q", str(work), head, env=env)
        return head


def find_base(engine, tree):
    """The newest first-parent ancestor of HEAD whose tree is the locked engine tree."""
    for line in git(engine, "rev-list", "--first-parent", "--format=%T", "HEAD").splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif line.strip() == tree:
            return commit
    return None


def uncommitted_records(root, engine_lock):
    """Lock or listed patch files that a run saved but did not commit (answered "no", or stopped)."""
    if not (root / ".git").exists():
        return []
    paths = ["source.lock.json", *engine_lock["patches"]]
    changed = git(root, "diff", "--name-only", "HEAD", "--", *paths, check=False).splitlines()
    added = git(root, "ls-files", "--others", "--exclude-standard", "--", *paths, check=False).splitlines()
    return sorted(set(changed + added))


def commit_records(root, engine, paths, my_identity):
    git(root, "add", "--", *paths)
    identity = (
        []
        if my_identity or git(root, "config", "--get", "user.email", check=False)
        else ["-c", "user.name=" + publisher(root), "-c", "user.email=" + ENGINE_EMAIL]
    )
    env = os.environ.copy()
    if identity:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = utc_now()
    subject = git(engine, "log", "-1", "--format=%s", "HEAD")
    git(root, *identity, "-c", "commit.gpgsign=false", "commit", "-q", "-m",
        f"Engine patches: {subject}", "--", *paths, env=env)  # fmt: skip
    return git(root, "rev-parse", "HEAD")


def update(root, message=None, answer=None, my_identity=False, commit_main=None, progress=None):
    progress = [] if progress is None else progress
    lock, crlf = read_lock(root)
    engine_lock = lock["cycles"]
    engine = root / "third_party/cycles"
    if not (engine / ".git").exists():
        raise Stop("third_party/cycles does not exist yet. Run step 2 (get sources and build) first.")
    tip = engine_lock.get("authoring_tip")

    def succeeds(*args):
        return subprocess.run(["git", "-C", str(engine), *args], capture_output=True).returncode == 0

    if tip and succeeds("cat-file", "-e", tip + "^{commit}") and succeeds("merge-base", "--is-ancestor", tip, "HEAD"):
        raise Stop("This is the private authoring fork; use scripts/export-engine-patches.py there.")

    # 1. Commit uncommitted engine edits.
    status = git(engine, "status", "--porcelain", "--untracked-files=all")
    if status:
        listed = subprocess.run(
            ["git", "-C", str(engine), "status", "--porcelain", "-z", "--untracked-files=all"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", errors="replace")
        # -z: "XY path" entries; a rename is followed by its old path as a separate entry.
        names, entries = [], listed.split("\0")
        while entries:
            entry = entries.pop(0)
            if len(entry) > 3:
                names.append(entry[3:])
                if entry[0] in "RC" and entries:
                    entries.pop(0)
        if any(name == "lib/windows_x64" or name.startswith("lib/windows_x64/") for name in names):
            raise Stop(
                "The dependency folder lib/windows_x64 changed. Undo that change; only engine source belongs in patches."
            )
        print("Changed engine files:")
        for line in status.splitlines():
            print("   " + line)
        if not ask("Save these changes as one new engine patch?", answer):
            raise Stop("Nothing was changed.")
        fixed = normalize_line_endings(engine, names)
        for name in fixed:
            print(f"   (saved {name} with Unix line endings, as the engine keeps them)")
        if message is None and answer is None:
            try:
                message = input("Describe the change in one line (Enter for a default): ").strip()
            except EOFError:
                message = ""
        message = message or f"Engine change by {publisher(root)}"
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = utc_now()
        if not my_identity:
            # The patch file shows this name and address; the default keeps your own e-mail private.
            for role in ("AUTHOR", "COMMITTER"):
                env[f"GIT_{role}_NAME"], env[f"GIT_{role}_EMAIL"] = publisher(root), ENGINE_EMAIL
        git(engine, "add", "-A")
        git(engine, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)
        progress.append("your engine edit is saved as engine commit " + git(engine, "rev-parse", "--short", "HEAD"))

    # 2. Find the commits made after the locked engine.
    head_tree = git(engine, "rev-parse", "HEAD^{tree}")
    if head_tree == engine_lock.get("authoring_tree"):
        result = dict(result="unchanged", message="The patches already describe this engine; nothing to do.")
        # A run that saved the records but did not commit them (it stopped, or the answer was "no").
        pending = uncommitted_records(root, engine_lock)
        if pending and commit_main is not False:
            print("source.lock.json and patch files are saved but not committed: " + ", ".join(pending))
            if commit_main or ask("Commit them in this folder now?", answer):
                result["committed"] = commit_records(root, engine, pending, my_identity)
                result["message"] = "The saved patch files and source.lock.json were committed."
        return result
    base = find_base(engine, engine_lock["authoring_tree"])
    if base is None:
        raise Stop(
            "The engine does not grow out of the engine that source.lock.json describes (cycles.authoring_tree). "
            "This happens when the sources were updated while the engine had other changes. Move third_party/cycles "
            "aside, run step 2 again, and repeat your engine change there."
        )
    revision = base + "..HEAD"
    if git(engine, "rev-list", "--merges", revision):
        raise Stop("The engine history contains a merge; patches need a straight line of commits.")
    commits = git(engine, "rev-list", "--reverse", revision).split()
    for commit in commits:
        if not git(engine, "diff", "--name-only", commit + "^", commit):
            raise Stop(f"Engine commit {commit[:10]} changes nothing; remove it (an empty patch cannot be applied).")
        email = git(engine, "log", "-1", "--format=%ae", commit)
        if email != ENGINE_EMAIL and not re.fullmatch(
            r"[a-z][a-z-]*@localhost|[^@]+@example\.(invalid|org|com|net)", email
        ):
            print(
                f"Note: commit {commit[:10]} carries the e-mail address {email}; it will be visible in the patch file."
            )
            if answer:
                print("(--yes does not answer this question: only a person can agree to publish an address.)")
            if not ask("Continue with this address?", None):
                raise Stop("Nothing was exported. Rewrite the commit's author, or use the tool's own identity.")

    # 3. Export and check.
    start = next_patch_number(root, engine_lock)
    with tempfile.TemporaryDirectory(prefix="cycles-patches-") as temporary:
        out = Path(temporary) / "patches"
        out.mkdir()
        git(
            engine,
            "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "format.signature=",
            "-c", "format.numbered=auto", "-c", "core.quotePath=true",
            "format-patch", "--binary", "--full-index", "--no-signature", "--no-stat",
            f"--start-number={start}", "--output-directory=" + str(out), revision,
        )  # fmt: skip
        patches = sorted(out.glob("*.patch"))
        if len(patches) != len(commits):
            raise Stop("The number of exported patches differs from the number of engine commits.")
        # Patch files not listed in the lock are left over from a run that stopped before it
        # wrote source.lock.json. The same files again are fine (the run continues); others stop.
        known = set(engine_lock["patches"])
        triage = root / "patches/triage.json"
        if triage.is_file():
            known.update(
                row["path"] for row in json.loads(triage.read_text(encoding="utf-8")).get("retired_patches", [])
            )
        exported = {"patches/" + patch.name for patch in patches}
        for patch in patches:
            patch.write_bytes(utc_patch_dates(patch.read_bytes()))
        for stray in sorted((root / "patches").glob("*.patch")):
            name = "patches/" + stray.name
            if name not in known and name not in exported:
                raise Stop(
                    f"{name} is not listed in source.lock.json (left over from an interrupted run?). "
                    "Move it out of patches/ and run this again; nothing else was changed."
                )
        for patch in patches:
            if b"\r\n" in patch.read_bytes():
                raise Stop(
                    f"{patch.name} contains Windows line endings, which patches/ cannot keep. Save the changed "
                    "file with Unix (LF) line endings and run this again."
                )
            existing = root / "patches" / patch.name
            if existing.exists() and ("patches/" + patch.name in known or existing.read_bytes() != patch.read_bytes()):
                raise Stop(
                    f"patches/{patch.name} already exists with other content; move it out of patches/ and run "
                    "this again."
                )
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "replay.index")
        git(engine, "read-tree", base, env=env)
        for patch in patches:
            git(engine, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env)
        if git(engine, "write-tree", env=env) != head_tree:
            raise Stop("The exported patches do not reproduce the engine; nothing was changed.")
        # The engine's own commits become the ones a fresh clone will make (same files).
        try:
            canonical = canonical_commits(engine, base, [patch.read_bytes() for patch in patches])
        except (Stop, subprocess.CalledProcessError) as error:
            raise Stop("Checking the patches like a fresh clone failed; nothing was changed. " + str(error)) from None
        if git(engine, "rev-parse", canonical + "^{tree}") != head_tree:
            raise Stop("A fresh clone would build a different engine from these patches; nothing was changed.")
        # 4. Record.
        added = []
        for patch in patches:
            data = patch.read_bytes()
            name = "patches/" + patch.name
            (root / name).write_bytes(data)
            added.append(dict(path=name, sha256=hashlib.sha256(data).hexdigest()))
    engine_lock["patches"].extend(item["path"] for item in added)
    engine_lock["patch_sha256"].update({item["path"]: item["sha256"] for item in added})
    engine_lock["authoring_tree"] = head_tree
    write_lock(root, lock, crlf)
    progress.append("patches/ and source.lock.json describe the edited engine")
    # Same tree, so only the commit ids change; files, index and working tree stay as they are.
    old_head = git(engine, "rev-parse", "HEAD")
    branch = git(engine, "symbolic-ref", "-q", "HEAD", check=False)
    git(engine, "update-ref", "-m", "update-engine-patches: commits as a fresh clone makes them",
        branch or "HEAD", canonical, old_head)  # fmt: skip
    result = dict(
        result="passed", added=added, authoring_tree=head_tree, engine_commits=len(commits),
        engine_head=canonical, previous_engine_head=old_head,
    )  # fmt: skip
    if commit_main is not False and not (root / ".git").exists():
        print("This folder is not a Git checkout, so patches/ and source.lock.json were saved but not committed.")
        commit_main = False
    if commit_main is None:
        commit_main = ask("Commit the new patch files and source.lock.json in this folder now?", answer)
    if commit_main:
        result["committed"] = commit_records(
            root, engine, ["source.lock.json", *[item["path"] for item in added]], my_identity
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--message", help="One-line description for uncommitted engine edits")
    parser.add_argument("--yes", action="store_true", help="Answer yes to every question")
    parser.add_argument("--no-commit", action="store_true", help="Do not commit patches/ and source.lock.json")
    parser.add_argument("--my-git-identity", action="store_true", help="Commit with your own Git name and e-mail")
    args = parser.parse_args(argv)
    progress = []
    try:
        result = update(
            args.source_root.resolve(),
            args.message,
            True if args.yes else None,
            args.my_git_identity,
            False if args.no_commit else (True if args.yes else None),
            progress,
        )
    except Stop as error:
        # Say plainly what was already done, so "run it again" is not a surprise.
        done = progress or ["nothing was changed"]
        print(json.dumps(dict(result="failed", error=str(error), already_done=done), indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
