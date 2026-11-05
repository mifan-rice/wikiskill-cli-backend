"""Tests for review.py — human review queue (no network, no real agent).

Run: <python> tests/test_review.py
Covers: apply_edits fail-loud contract / queue persistence / sync idempotency /
        provenance-based adoption / dismiss persistence / adopt dry-run,
        publish, name whitelist, missing patch target, provenance accumulation.
"""

import json
import os
import shutil
import sys
import tempfile

# 同 smoke.py：英文 locale 的 Windows runner 默认 cp1252，打印非 ASCII 会抛
# UnicodeEncodeError 并中断整轮测试。强制 UTF-8 stdio，保证跨主机一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 非 TextIOWrapper 或流已关闭时跳过
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import review  # noqa: E402
from review import (STATUS_ADOPTED, STATUS_DISMISSED, STATUS_PENDING,  # noqa: E402
                    Review, apply_edits, load_queue, save_queue)

fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        fails.append(name)


def tmpdir():
    return tempfile.mkdtemp(prefix="review-test-")


def make_ws(tmp):
    ws = os.path.join(tmp, "ws")
    os.makedirs(os.path.join(ws, "runs", "proposals"))
    os.makedirs(os.path.join(ws, "skills"))
    return ws


def write_proposal(ws, it, action="create", name="demo-skill", **extra):
    payload = {"action": action, "name": name}
    payload.update(extra)
    p = os.path.join(ws, "runs", "proposals", f"iter-{it:02d}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return p


def write_state(ws, rows):
    p = os.path.join(ws, "runs", "state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"history": rows}, f)


def write_purpose(ws, name, lines):
    d = os.path.join(ws, "skills", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "PURPOSE.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def status_of(r, it):
    data = load_queue(r.queue_path())
    for row in data["items"]:
        if row.get("iter") == it:
            return row.get("status")
    return None


def test_apply_edits():
    print("[apply_edits]")
    check("append", apply_edits("a\n", [{"op": "append", "content": "b"}])
          == "a\n\nb\n")
    check("replace unique", apply_edits("x 1 x 2", [
        {"op": "replace", "target": "x 2", "content": "y"}]) == "x 1 y")
    check("insert_after", apply_edits("head\n\nbody", [
        {"op": "insert_after", "target": "head", "content": "mid"}])
          == "head\nmid\n\nbody")
    for label, edits in (
        ("missing target", [{"op": "replace", "target": "nope", "content": "y"}]),
        ("ambiguous target", [{"op": "replace", "target": "x", "content": "y"}]),
        ("blank target", [{"op": "insert_after", "target": "", "content": "y"}]),
        ("unknown op", [{"op": "rename", "content": "y"}]),
    ):
        try:
            apply_edits("x x", edits)
            check(f"{label} raises", False)
        except ValueError:
            check(f"{label} raises", True)


def test_queue_persistence():
    print("[queue persistence]")
    tmp = tmpdir()
    try:
        p = os.path.join(tmp, "runs", "review-queue.json")
        check("missing file -> empty skeleton",
              load_queue(p) == {"updated_at": None, "items": []})
        data = {"updated_at": review._stamp(),
                "items": [{"iter": 1, "status": STATUS_PENDING}]}
        check("save returns True", save_queue(data, p) is True)
        check("round-trip", load_queue(p)["items"][0]["iter"] == 1)
        check("no .tmp left", not os.path.exists(p + ".tmp"))
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        check("corrupt file -> empty skeleton",
              load_queue(p)["items"] == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sync_gate_and_pending():
    print("[sync / gate]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 1, action="create", name="alpha", skill_md="# a")
        write_proposal(ws, 2, action="patch", name="alpha",
                       edits=[{"op": "append", "content": "b"}])
        write_proposal(ws, 3, action="no_action")
        write_state(ws, [{"iter": 1, "accepted": True, "r_val": 0.7},
                         {"iter": 2, "accepted": False, "r_val": 0.4}])
        r = Review(ws)
        data = r.sync()
        check("only create/patch queued", len(data["items"]) == 2,
              [i["iter"] for i in data["items"]])
        check("added counts first sight", data["added"] == 2, data["added"])
        check("gate accept -> adopted", status_of(r, 1) == STATUS_ADOPTED)
        check("gate reject -> pending", status_of(r, 2) == STATUS_PENDING)
        again = r.sync()
        check("sync idempotent (no re-add)", again["added"] == 0, again["added"])
        check("gate accept still adopted", status_of(r, 1) == STATUS_ADOPTED)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_publish_requires_human():
    print("[auto_publish]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 1, action="create", name="alpha", skill_md="# a")
        write_state(ws, [{"iter": 1, "accepted": True, "r_val": 0.7}])
        r = Review(ws, auto_publish=True)
        r.sync()
        check("gate accept stays pending in auto mode",
              status_of(r, 1) == STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_provenance_not_names():
    print("[provenance]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 3, action="patch", name="shared-skill")
        write_proposal(ws, 7, action="patch", name="shared-skill")
        write_state(ws, [{"iter": 3, "accepted": False},
                         {"iter": 7, "accepted": False}])
        write_purpose(ws, "shared-skill", [
            "# shared-skill",
            "Adopted: human-gate 2026-10-17 10:00 from iter-03 (gate=reject)"])
        r = Review(ws)
        r.sync()
        check("landed iter -> adopted", status_of(r, 3) == STATUS_ADOPTED)
        check("same-name later patch stays pending",
              status_of(r, 7) == STATUS_PENDING)

        # Remove the provenance line: the adopted row falls back to pending.
        write_purpose(ws, "shared-skill", ["# shared-skill (rewritten)"])
        r.sync()
        check("lost evidence -> pending", status_of(r, 3) == STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dismiss_persists():
    print("[dismiss]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 2, action="create", name="beta", skill_md="# b")
        write_state(ws, [{"iter": 2, "accepted": False}])
        r = Review(ws)
        r.sync()
        check("dismiss hits one row", r.set_status(2, STATUS_DISMISSED,
                                                   reason="bench-specific")
              == 1)
        r.sync()
        check("dismiss survives sync", status_of(r, 2) == STATUS_DISMISSED)
        check("pending() excludes dismissed", r.pending() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_publication_conflict():
    print("[publication conflict]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 4, action="patch", name="gamma")
        write_state(ws, [{"iter": 4, "accepted": True}])
        r = Review(ws)
        r.record_publication_conflict(4, "gamma", "target file edited locally")
        check("conflict row pending", status_of(r, 4) == STATUS_PENDING)
        row = [i for i in load_queue(r.queue_path())["items"]
               if i.get("iter") == 4][0]
        check("conflict reason kept",
              row.get("publication_conflict") == "target file edited locally")
        r.sync()
        check("conflict not auto-adopted by gate accept",
              status_of(r, 4) == STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_adopt_create():
    print("[adopt create]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 1, action="create", name="new-skill",
                       skill_md="# new skill\nDo the thing.\n",
                       purpose_md="Why it exists.")
        write_state(ws, [{"iter": 1, "accepted": False, "r_val": 0.3,
                          "train_mean": 0.2}])
        r = Review(ws)
        rc = r.adopt(1, dry_run=True)
        check("dry-run rc 0", rc == 0, rc)
        check("dry-run writes nothing",
              not os.path.exists(os.path.join(ws, "skills", "new-skill")))
        rc = r.adopt(1)
        dst = os.path.join(ws, "skills", "new-skill", "SKILL.md")
        purpose = os.path.join(ws, "skills", "new-skill", "PURPOSE.md")
        check("adopt rc 0", rc == 0, rc)
        check("SKILL.md written", os.path.isfile(dst))
        body = open(dst, encoding="utf-8").read()
        check("SKILL.md body", body.startswith("# new skill"))
        check("PURPOSE.md has provenance",
              "Adopted: human-gate" in open(purpose, encoding="utf-8").read()
              and "from iter-01" in open(purpose, encoding="utf-8").read())
        check("queue row adopted", status_of(r, 1) == STATUS_ADOPTED)

        # A second adoption of the same skill keeps the older record.
        write_proposal(ws, 2, action="patch", name="new-skill",
                       edits=[{"op": "append", "content": "More."}])
        write_state(ws, [{"iter": 1, "accepted": False},
                         {"iter": 2, "accepted": True}])
        check("patch adopt rc 0", r.adopt(2) == 0)
        txt = open(purpose, encoding="utf-8").read()
        check("both provenance lines kept",
              "from iter-01" in txt and "from iter-02" in txt)
        check("patch applied",
              "More." in open(dst, encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_adopt_rejections():
    print("[adopt rejections]")
    tmp = tmpdir()
    try:
        ws = make_ws(tmp)
        write_proposal(ws, 1, action="create", name="../../escape",
                       skill_md="# x")
        write_proposal(ws, 2, action="patch", name="missing-skill",
                       edits=[{"op": "append", "content": "x"}])
        write_proposal(ws, 3, action="no_action")
        r = Review(ws)
        check("illegal name rc 2", r.adopt(1) == 2)
        check("missing patch target rc 2", r.adopt(2) == 2)
        check("no_action rc 2", r.adopt(3) == 2)
        check("unknown iter rc 2", r.adopt(99) == 2)
        check("nothing written", os.listdir(os.path.join(ws, "skills")) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_apply_edits()
    test_queue_persistence()
    test_sync_gate_and_pending()
    test_auto_publish_requires_human()
    test_provenance_not_names()
    test_dismiss_persists()
    test_publication_conflict()
    test_adopt_create()
    test_adopt_rejections()
    print()
    print(f"checks {checks}, failures {len(fails)}")
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
