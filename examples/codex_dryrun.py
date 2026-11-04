"""用真实 CLI 配置跑 dry_run：只到「argv 组装 + prompt 落盘」为止，不执行、不出网。

用途：把 examples/codex.json 这类真实配置接上后，先证明占位符、技能注入、
stdin 策略都按预期展开，再去真跑模型（真跑要执行层放行该 exe + 放行网络）。

跑法：<python> examples/codex_dryrun.py
"""

import json
import os
import shlex
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend import CliBackend  # noqa: E402

WS = os.path.join(ROOT, ".tmp-ws-codex")


def main() -> int:
    with open(os.path.join(HERE, "codex.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if os.path.isdir(WS):
        shutil.rmtree(WS)
    for rel, body in {
        "skills/active/demo-skill/SKILL.md": "# demo skill\nDo X then Y.\n",
        "skills/framework/wiki-maintainer/SKILL.md": "# maintainer\n",
        "wiki/patterns/.keep": "",
        "wiki/index.md": "# index\n",
    }.items():
        p = os.path.join(WS, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)

    b = CliBackend(cfg)
    r = b.run(WS, "整理 wiki 里关于 flaky test 的模式页", tag="iter-1-inference",
              dry_run=True)
    print("argv:", shlex.join(r.cmd))
    print("stdin:", cfg.get("stdin") or "(empty, closes immediately)")
    print("run_dir:", r.extra.get("run_dir"))
    print("prompt_file:", r.extra.get("prompt_file"))
    print("prompt_chars:", r.extra.get("prompt_chars"))
    head = open(r.extra["prompt_file"], encoding="utf-8").read()[:300]
    print("--- prompt 前 300 字 ---")
    print(head)
    missing = [p for p in ("skill", "SKILL") if p not in head]
    print("--- 占位符残留检查 ---")
    leftovers = [x for x in r.cmd if x.startswith("{") and x.endswith("}")]
    print("leftover placeholders:", leftovers or "none")
    return 0 if not leftovers and r.extra.get("prompt_chars", 0) > 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
