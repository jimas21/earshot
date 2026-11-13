#!/usr/bin/env python3
"""Compare compiler or cppcheck warnings between the fork point and HEAD.

  warn_diff.py --title T --head head.log --head-root DIR --base base.log --base-root DIR \
               --changed changed.txt

Prints a Markdown summary. Exits 1 if a file we changed since forking has a warning
the fork point did not have (same file, same message; line numbers are ignored so moved
code is not "new"). Warnings in upstream-only files never fail the job.
"""
import argparse
import collections
import os
import re
import sys

LINE = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:\d+:)? "
                  r"(?P<sev>warning|style|performance|portability|error): (?P<msg>.*)$")


def parse(path, root):
    root = os.path.realpath(root).replace("\\", "/").rstrip("/") + "/"
    found = []
    with open(path, errors="replace") as fh:
        for raw in fh:
            m = LINE.match(raw.rstrip("\n"))
            if not m:
                continue
            f = m.group("file").replace("\\", "/")
            if f.startswith(root):
                f = f[len(root):]
            f = re.sub(r"^(\./)+", "", f)
            if not f.startswith("src/") and not f.startswith("test/"):
                continue  # fetched dependencies and system headers
            found.append((f, int(m.group("line")), m.group("sev"), m.group("msg")))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--head-root", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--base-root", required=True)
    ap.add_argument("--changed", required=True)
    a = ap.parse_args()

    head = parse(a.head, a.head_root)
    base = parse(a.base, a.base_root)
    changed = {l.strip() for l in open(a.changed) if l.strip()}

    head_unique = sorted(set(head))
    base_keys = collections.Counter((f, m) for f, _, _, m in set(base))
    new = []
    seen = collections.Counter()
    for f, line, sev, msg in head_unique:
        if f not in changed:
            continue
        seen[(f, msg)] += 1
        if seen[(f, msg)] > base_keys[(f, msg)]:
            new.append((f, line, sev, msg))

    by_file = collections.Counter(f for f, _, _, _ in head_unique)
    out = [f"### {a.title}", "",
           f"- warnings at HEAD (src/ and test/, de-duplicated): **{len(head_unique)}**",
           f"- warnings at the fork point, same configuration: **{len(set(base))}**",
           f"- new warnings in files changed since forking: **{len(new)}**", ""]
    if new:
        out += ["#### New in files we changed (these fail the job)", "```"]
        out += [f"{f}:{l}: {s}: {m}" for f, l, s, m in new]
        out += ["```", ""]
    out += ["<details><summary>All warnings at HEAD, by file</summary>", "", "```"]
    for f, n in by_file.most_common():
        tag = " (changed by us)" if f in changed else ""
        out.append(f"{n:4d}  {f}{tag}")
    out += ["```", "", "```"]
    out += [f"{f}:{l}: {s}: {m}" for f, l, s, m in head_unique]
    out += ["```", "</details>"]
    print("\n".join(out))
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
