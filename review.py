"""review.py — human review queue for WikiSkill evolution proposals.

The upstream auto channel is a strict gate: a proposal is published only when
``R_val > R_best`` on a validation bench. Proposals driven by real-world
material usually cannot clear such a gate on a small self-built bench — not
because the experience is worthless, but because the gate measures a different
thing. Dropping those proposals silently loses the experience.

This module adds the *side door*: every ``create`` / ``patch`` proposal that
is not published by the gate is kept in ``<workspace>/runs/review-queue.json``
with an explicit status, so a human can adopt or dismiss it later:

    pending    awaiting a human decision
    adopted    published to the skill library (with provenance in PURPOSE.md)
    dismissed  looked at, deliberately not published

Design rules (each one exists because its absence caused a real bug):

* **Provenance, not names.** Whether iter-N was published is read from
  ``PURPOSE.md`` lines (``Adopted: human-gate ... from iter-N``). Using
  "a skill with this name already exists" mislabels every ``patch`` proposal
  that shares a name with an older adoption.
* **Idempotent sync.** ``sync()`` can be called any number of times; a
  ``dismissed`` row stays dismissed, and an ``adopted`` row that loses its
  evidence falls back to ``pending`` instead of silently disappearing.
* **Fail-loud edits.** ``patch`` edits must match exactly once; zero or
  multiple matches abort without writing anything.
* **Atomic writes.** Queue writes go through a temp file + ``os.replace``;
  a crash never leaves a truncated JSON behind.
* **No implicit commits.** ``adopt`` writes files; ``git add/commit`` only
  happens with ``commit=True`` so a public checkout never commits into the
  caller's repository by surprise.

CLI::

    python review.py queue  --workspace /path/to/evo-workspace
    python review.py list   --workspace /path/to/evo-workspace
    python review.py adopt  --workspace ... 3 --dry-run
    python review.py dismiss --workspace ... 3 --reason "bench-specific trick"
    python review.py export --workspace ...

License: MIT (see LICENSE).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess

QUEUE_NAME = "review-queue.json"
STATUS_PENDING = "pending"
STATUS_ADOPTED = "adopted"
STATUS_DISMISSED = "dismissed"
STATUS_FAILED = "failed"

#: Skill names are used as directory names: keep them boring (no traversal).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PROP_RE = re.compile(r"^iter-(\d+)\.json$")
HUMAN_ADOPTION_RE = re.compile(
    r"^Adopted: human-gate [^\r\n]*? from iter-(\d+)\b[^\r\n]*", re.MULTILINE)
AUTO_ADOPTION_RE = re.compile(
    r"^Adopted: auto-review [^\r\n]*? from iter-(\d+)\b[^\r\n]*", re.MULTILINE)


def _stamp() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Editing proposals (pure functions)
# ---------------------------------------------------------------------------

def apply_edits(text: str, edits: list) -> str:
    """Apply a ``patch`` proposal's edits to a SKILL.md body (fail loudly).

    Supported ops: ``append`` (end of file), ``replace`` / ``insert_after``
    (target must match exactly once). Anything else raises ``ValueError``.
    """
    for i, e in enumerate(edits or []):
        op = e.get("op")
        content = e.get("content", "")
        if op == "append":
            text = text.rstrip() + "\n\n" + content + "\n"
            continue
        if op in ("replace", "insert_after"):
            t = e.get("target") or ""
            if not t:
                raise ValueError(f"edit[{i}] {op}: missing target")
            cnt = text.count(t)
            if cnt == 0:
                raise ValueError(f"edit[{i}] {op}: target not found: {t[:60]!r}")
            if cnt > 1:
                raise ValueError(
                    f"edit[{i}] {op}: target matches {cnt} places "
                    f"(need exactly one): {t[:60]!r}")
            if op == "replace":
                text = text.replace(t, content)
            else:
                k = text.index(t) + len(t)
                text = text[:k] + "\n" + content + text[k:]
            continue
        raise ValueError(f"edit[{i}]: unknown op {op!r}")
    return text


# ---------------------------------------------------------------------------
# Queue persistence
# ---------------------------------------------------------------------------

def load_queue(path: str) -> dict:
    """Read the queue; a missing or corrupt file yields an empty skeleton."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data.setdefault("updated_at", None)
            return data
    except (OSError, ValueError):
        pass
    return {"updated_at": None, "items": []}


def save_queue(data: dict, path: str) -> bool:
    """Atomic write (temp file + replace); warns instead of raising."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"[review] warning: queue not written: {e}")
        return False


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class Review:
    """Review queue bound to one evolution workspace.

    Args:
        workspace:   evolution workspace (contains ``runs/`` and ``skills/``).
        skills_root: target skill library. Defaults to ``<workspace>/skills``.
        auto_publish: when true, a gate-accepted proposal is *not* considered
            published on the gate alone — a human still has to adopt it. Use
            this when an automatic quality check (instead of the gate) decides
            publication. Defaults to False (gate acceptance publishes).
    """

    def __init__(self, workspace: str, skills_root: str = None,
                 auto_publish: bool = False):
        self.workspace = os.path.abspath(workspace)
        self.skills_root = os.path.abspath(skills_root or
                                           os.path.join(self.workspace, "skills"))
        self.auto_publish = bool(auto_publish)

    # ---------------- inputs ----------------

    def proposals(self) -> list:
        """All proposals as ``(iter, path, payload)``; ``no_action`` included."""
        d = os.path.join(self.workspace, "runs", "proposals")
        out = []
        if not os.path.isdir(d):
            return out
        for n in sorted(os.listdir(d)):
            m = PROP_RE.match(n)
            if not m:
                continue
            p = os.path.join(d, n)
            try:
                with open(p, encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError) as e:
                payload = {"action": "?unreadable",
                           "error": f"{type(e).__name__}: {e}"}
            out.append((int(m.group(1)), p, payload))
        out.sort(key=lambda x: x[0])
        return out

    def state_history(self) -> dict:
        """iteration -> state.json history entry (the gate verdict)."""
        p = os.path.join(self.workspace, "runs", "state.json")
        if not os.path.isfile(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return {}
        return {h.get("iter"): h for h in (st.get("history") or [])}

    def adopted_names(self) -> set:
        """Skill names carrying a PURPOSE.md (i.e. evolution output)."""
        out = set()
        if not os.path.isdir(self.skills_root):
            return out
        for n in os.listdir(self.skills_root):
            if os.path.isfile(os.path.join(self.skills_root, n, "PURPOSE.md")):
                out.add(n)
        return out

    def adopted_iters(self) -> set:
        """Iteration numbers with an explicit adoption line in PURPOSE.md."""
        out = set()
        if not os.path.isdir(self.skills_root):
            return out
        for n in os.listdir(self.skills_root):
            p = os.path.join(self.skills_root, n, "PURPOSE.md")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            for pattern in (HUMAN_ADOPTION_RE, AUTO_ADOPTION_RE):
                for m in pattern.finditer(txt):
                    out.add(int(m.group(1)))
        return out

    def queue_path(self) -> str:
        return os.path.join(self.workspace, "runs", QUEUE_NAME)

    # ---------------- queue ----------------

    def sync(self) -> dict:
        """Reconcile proposals against adoption records; keep human decisions.

        Returns the queue dict with an extra ``added`` count (rows first seen).
        """
        hist = self.state_history()
        adopted_iters = self.adopted_iters()
        data = load_queue(self.queue_path())
        items = {}
        for it in data.get("items", []):
            if isinstance(it, dict) and isinstance(it.get("iter"), int):
                items[it["iter"]] = it
        added = 0
        for it, _p, pay in self.proposals():
            if pay.get("action") not in ("create", "patch"):
                continue
            name = pay.get("name") or ""
            h = hist.get(it) or {}
            gate_acc = bool(h.get("accepted"))
            landed = it in adopted_iters
            cur = items.get(it)
            if cur is None:
                cur = {"iter": it, "status": STATUS_PENDING,
                       "status_at": None, "reason": ""}
                items[it] = cur
                added += 1
            cur.update({
                "action": pay.get("action"),
                "name": name,
                "gate": ("accept" if gate_acc else
                         ("reject" if h.get("accepted") is False else "-")),
                "r_val": h.get("r_val"),
                "train_mean": h.get("train_mean"),
                "purpose_head": (pay.get("purpose_md") or "").strip()[:200],
            })
            # A published-by-gate row recorded before auto_publish was enabled
            # stays adopted (migration); new gate accepts in auto mode wait.
            legacy_record = cur.get("status") == STATUS_ADOPTED
            if landed or ((not self.auto_publish or legacy_record)
                          and gate_acc and not cur.get("publication_conflict")):
                cur.pop("publication_conflict", None)
                if cur.get("status") != STATUS_ADOPTED:
                    cur["status"] = STATUS_ADOPTED
                    cur["status_at"] = _stamp()
            elif (cur.get("publication_conflict")
                  and cur.get("status") not in (STATUS_DISMISSED, STATUS_FAILED)):
                cur["status"] = STATUS_PENDING
            elif cur.get("status") == STATUS_ADOPTED:
                # Evidence is gone (file deleted / line edited): back to pending.
                cur["status"] = STATUS_PENDING
                cur["status_at"] = _stamp()
            elif cur.get("status") not in (STATUS_DISMISSED, STATUS_FAILED):
                cur["status"] = STATUS_PENDING
        out = {"updated_at": _stamp(),
               "items": [items[k] for k in sorted(items)]}
        save_queue(out, self.queue_path())
        out["added"] = added
        return out

    def record_publication_conflict(self, iter_no: int, name: str,
                                    reason: str) -> None:
        """Gate passed but publishing failed: surface it as pending, loudly."""
        data = load_queue(self.queue_path())
        row = next((i for i in data["items"]
                    if isinstance(i, dict) and i.get("iter") == iter_no), None)
        if row is None:
            row = {"iter": iter_no, "name": name, "action": "patch",
                   "gate": "accept"}
            data["items"].append(row)
        row.update(status=STATUS_PENDING, status_at=_stamp(), reason=reason,
                   publication_conflict=reason)
        data["updated_at"] = _stamp()
        if not save_queue(data, self.queue_path()):
            raise OSError("publication conflict could not be queued for review")

    def set_status(self, iter_no: int, status: str, reason: str = "") -> int:
        """Change one row's status (adopt/dismiss call this). Returns rows hit."""
        data = load_queue(self.queue_path())
        if not any(isinstance(it, dict) and it.get("iter") == iter_no
                   for it in data.get("items", [])):
            self.sync()
            data = load_queue(self.queue_path())
        hit = 0
        for it in data.get("items", []):
            if isinstance(it, dict) and it.get("iter") == iter_no:
                it["status"] = status
                if status == STATUS_ADOPTED:
                    it.pop("publication_conflict", None)
                it["status_at"] = _stamp()
                if reason:
                    it["reason"] = reason
                hit += 1
        if hit:
            data["updated_at"] = _stamp()
            save_queue(data, self.queue_path())
        return hit

    def pending(self) -> list:
        return [it for it in load_queue(self.queue_path()).get("items", [])
                if isinstance(it, dict) and it.get("status") == STATUS_PENDING]

    # ---------------- adopt (human door) ----------------

    def adopt(self, iter_no: int, dry_run: bool = False,
              commit: bool = False) -> int:
        """Publish one proposal to the skill library. Returns an exit code."""
        props = {it: pay for it, _p, pay in self.proposals()}
        pay = props.get(iter_no)
        if pay is None:
            print(f"x no iter-{iter_no:02d} proposal")
            return 2
        action = pay.get("action")
        name = pay.get("name") or ""
        if action not in ("create", "patch"):
            print(f"x iter-{iter_no:02d} is {action}; nothing to publish")
            return 2
        if not NAME_RE.match(name):
            print(f"x illegal skill name (^[a-z0-9][a-z0-9_-]*$): {name!r}")
            return 2
        dst_dir = os.path.join(self.skills_root, name)
        dst_skill = os.path.join(dst_dir, "SKILL.md")
        h = self.state_history().get(iter_no) or {}
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        origin = (f"Adopted: human-gate {stamp} from iter-{iter_no:02d} "
                  f"(gate={'accept' if h.get('accepted') else 'reject'}, "
                  f"R_val={h.get('r_val')}, train_mean={h.get('train_mean')})")

        if action == "create":
            body = (pay.get("skill_md") or "").strip()
            if not body:
                print("x create proposal has no skill_md")
                return 2
        else:
            if not os.path.isfile(dst_skill):
                print(f"x patch target missing: {dst_skill}\n"
                      "  (patches target active skills; a missing target needs "
                      "a human decision, this tool never creates it silently)")
                return 2
            try:
                with open(dst_skill, encoding="utf-8") as f:
                    body = apply_edits(f.read(), pay.get("edits") or [])
            except (ValueError, OSError) as e:
                print(f"x patch failed (nothing written): {e}")
                return 3

        # PURPOSE.md describes this iteration; adoption records accumulate.
        # Overwriting them would make sync() re-open older adoptions of the
        # same skill name.
        ppath = os.path.join(dst_dir, "PURPOSE.md")
        records = {}
        try:
            with open(ppath, encoding="utf-8") as f:
                records = {int(m.group(1)): m.group(0).strip()
                           for m in HUMAN_ADOPTION_RE.finditer(f.read())}
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError) as e:
            print(f"x cannot read existing provenance (nothing written): {e}")
            return 3
        records[iter_no] = origin
        purpose = ((pay.get("purpose_md") or "").strip() + "\n\n---\n"
                   + "\n".join(records.values()) + "\n")

        if dry_run:
            print(f"[dry-run] action={action} name={name}")
            print(f"[dry-run] target: {dst_skill}")
            print(f"[dry-run] SKILL.md {len(body)} chars; PURPOSE.md "
                  f"{len(purpose)} chars")
            print("--- SKILL.md preview (first 800 chars) ---")
            print(body[:800])
            return 0

        os.makedirs(dst_dir, exist_ok=True)
        with open(dst_skill, "w", encoding="utf-8") as f:
            f.write(body if body.endswith("\n") else body + "\n")
        with open(ppath, "w", encoding="utf-8") as f:
            f.write(purpose)
        print(f"ok published: {dst_skill} ({os.path.getsize(dst_skill)} bytes)")
        print(f"ok provenance: {ppath}")

        rc = 0
        if commit:
            rc = self._commit(dst_dir, f"feat(skills): adopt {name} "
                                       f"from iter-{iter_no:02d}")
        try:
            self.set_status(iter_no, STATUS_ADOPTED, reason="human-gate adopt")
        except Exception as e:                                # noqa: BLE001
            print(f"[review] warning: queue status not updated: "
                  f"{type(e).__name__}: {e}")
        return rc

    def _commit(self, path: str, msg: str) -> int:
        """git add + commit exactly ``path`` (never the whole index)."""
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        rel = os.path.relpath(path, self.workspace).replace(os.sep, "/")
        r1 = subprocess.run(["git", "add", "--", rel], cwd=self.workspace,
                            timeout=30, capture_output=True, text=True,
                            check=False, env=env)
        r2 = subprocess.run(
            ["git", "-c", "user.name=wikiskill-review",
             "-c", "user.email=review@localhost", "commit", "-q", "-m", msg,
             "--", rel],
            cwd=self.workspace, timeout=60, capture_output=True, text=True,
            check=False, env=env)
        out = ((r2.stdout or "") + (r2.stderr or "")).strip()
        print(f"git add rc={r1.returncode} commit rc={r2.returncode}"
              + (f" | {out[:200]}" if out else ""))
        return 0 if r2.returncode == 0 else 1

    # ---------------- list / export ----------------

    def summary(self, payload: dict) -> str:
        a = payload.get("action")
        if a == "no_action":
            return "no_action"
        name = payload.get("name", "?")
        if a == "create":
            return f"create {name}"
        if a == "patch":
            return f"patch {name} ({len(payload.get('edits') or [])} edits)"
        return f"{a} {name}"

    def print_list(self) -> int:
        hist = self.state_history()
        props = self.proposals()
        if not props:
            print("no proposals: " + os.path.join(self.workspace, "runs",
                                                "proposals"))
            return 1
        adopted = self.adopted_names()
        print(f"workspace : {self.workspace}")
        print(f"skills    : {self.skills_root} ({len(adopted)} with PURPOSE.md)")
        print(f"{'iter':>4}  {'gate':<6} {'R_val':<7} {'train':<6} proposal")
        for it, _p, pay in props:
            h = hist.get(it) or {}
            gate = {True: "accept", False: "reject"}.get(h.get("accepted"), "-")
            mark = "*" if pay.get("action") in ("create", "patch") else " "
            print(f"{it:>4}  {gate:<6} {str(h.get('r_val'))[:6]:<7} "
                  f"{str(h.get('train_mean'))[:5]:<6} {mark}{self.summary(pay)}")
        print("* = real proposal (create/patch); "
              f"adopt: python review.py adopt --workspace {self.workspace} <iter>")
        return 0

    def export_doc(self, out_dir: str = None) -> str:
        hist = self.state_history()
        props = self.proposals()
        day = _dt.date.today().isoformat()
        out_dir = out_dir or os.path.join(self.workspace, "reports")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"review-{day}.md")
        lines = [f"# Proposals awaiting review - {day}", "",
                 f"- workspace: `{self.workspace}`",
                 f"- skill library: `{self.skills_root}`",
                 f"- adopt: `python review.py adopt --workspace "
                 f"{self.workspace} <iter>` (add `--dry-run` to preview)",
                 "- the gate is the automatic channel (`R_val > R_best`); this "
                 "document is the human channel: material-driven proposals "
                 "rarely clear a small bench, but the experience is still worth "
                 "publishing deliberately.", ""]
        real = [x for x in props if x[2].get("action") in ("create", "patch")]
        lines += ["## All proposals", "",
                  "| iter | action | skill | gate | R_val | train |",
                  "|---|---|---|---|---|---|"]
        for it, _p, pay in props:
            h = hist.get(it) or {}
            gate = {True: "accept", False: "reject"}.get(h.get("accepted"), "-")
            lines.append(f"| {it} | {pay.get('action')} | {pay.get('name', '-')} "
                         f"| {gate} | {h.get('r_val')} | {h.get('train_mean')} |")
        lines += ["", f"{len(real)} real proposal(s); the rest are no_action.",
                  ""]
        for it, _p, pay in real:
            h = hist.get(it) or {}
            lines += [f"## iter-{it:02d} - {pay.get('action')} - "
                      f"`{pay.get('name')}`", "",
                      f"- gate: {'accept' if h.get('accepted') else 'reject'}"
                      f" (R_val={h.get('r_val')} / "
                      f"train_mean={h.get('train_mean')})",
                      "- purpose.md:", "", "```text",
                      (pay.get("purpose_md") or "").strip()[:2000], "```", ""]
            if pay.get("action") == "create":
                lines += ["- skill.md (proposed body):", "", "```markdown",
                          (pay.get("skill_md") or "").strip(), "```", ""]
            else:
                lines += ["- edits:", ""]
                for e in (pay.get("edits") or []):
                    lines += [f"  - op=`{e.get('op')}` "
                              f"target={e.get('target')!r}", "", "```text",
                              (e.get("content") or "").strip()[:1500], "```", ""]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"exported: {path} ({os.path.getsize(path)} bytes, "
              f"{len(props)} proposals / {len(real)} real)")
        return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _review_from_args(a) -> Review:
    ws = a.workspace or os.environ.get("WIKISKILL_WS")
    if not ws:
        raise SystemExit("--workspace is required (or set WIKISKILL_WS)")
    return Review(ws, skills_root=a.skills,
                  auto_publish=bool(a.auto_publish))


def cmd_list(a) -> int:
    return _review_from_args(a).print_list()


def cmd_queue(a) -> int:
    r = _review_from_args(a)
    data = r.sync()
    items = data.get("items", [])
    n_pending = len([i for i in items if i.get("status") == STATUS_PENDING])
    n_adopt = len([i for i in items if i.get("status") == STATUS_ADOPTED])
    n_dism = len([i for i in items if i.get("status") == STATUS_DISMISSED])
    show = items if a.all else [i for i in items
                                if i.get("status") == STATUS_PENDING]
    print(f"queue: {r.queue_path()}")
    print(f"{len(items)} real proposal(s) "
          f"(pending {n_pending} / adopted {n_adopt} / dismissed {n_dism})")
    if not show:
        print("(nothing pending)")
        return 0
    for it in show:
        print(f"  iter-{it.get('iter', 0):02d} [{it.get('status')}] "
              f"{it.get('action')} {it.get('name')} "
              f"gate={it.get('gate')} R_val={it.get('r_val')}")
    print("adopt: python review.py adopt --workspace <ws> <iter> | "
          "dismiss: ... dismiss <iter> --reason ...")
    return 0


def cmd_dismiss(a) -> int:
    r = _review_from_args(a)
    hit = r.set_status(int(a.iter), STATUS_DISMISSED,
                       reason=getattr(a, "reason", "") or "")
    if not hit:
        print(f"x iter-{int(a.iter):02d} is not in the queue "
              "(run `queue` to see what is)")
        return 2
    print(f"ok iter-{int(a.iter):02d} -> dismissed"
          + (f" ({a.reason})" if getattr(a, "reason", "") else ""))
    return 0


def cmd_adopt(a) -> int:
    return _review_from_args(a).adopt(int(a.iter), dry_run=a.dry_run,
                                      commit=a.commit)


def cmd_export(a) -> int:
    _review_from_args(a).export_doc(a.out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="review",
        description="Human review queue / side door for WikiSkill proposals")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default=None,
                        help="evolution workspace (or WIKISKILL_WS)")
    common.add_argument("--skills", default=None,
                        help="skill library (default: <workspace>/skills)")
    common.add_argument("--auto-publish", action="store_true",
                        help="gate acceptance alone does not publish; "
                             "requires human adopt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", parents=[common],
                   help="list proposals with the gate verdict")
    pq = sub.add_parser("queue", parents=[common],
                        help="list the review queue (default: pending only)")
    pq.add_argument("--all", action="store_true",
                    help="include adopted / dismissed rows")
    pa = sub.add_parser("adopt", parents=[common],
                        help="(human door) publish one proposal")
    pa.add_argument("iter", type=int, help="iteration number (iter-NN)")
    pa.add_argument("--dry-run", action="store_true",
                    help="preview without writing")
    pa.add_argument("--commit", action="store_true",
                    help="git add/commit the published skill (default: no)")
    pd = sub.add_parser("dismiss", parents=[common],
                        help="mark a proposal as looked-at and not published")
    pd.add_argument("iter", type=int, help="iteration number")
    pd.add_argument("--reason", default="", help="why (optional)")
    pe = sub.add_parser("export", parents=[common],
                        help="write a markdown review document")
    pe.add_argument("--out", default=None, help="output directory")
    a = ap.parse_args(argv)
    return {"list": cmd_list, "queue": cmd_queue, "adopt": cmd_adopt,
            "dismiss": cmd_dismiss, "export": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
