"""夹具：一个假的 agent CLI，用来验证 CliBackend 的协议实现（不是真实 agent）。

行为（按 --tag 决定，模拟 wikiskill 的三种轮次）：
  * 推理轮（tag 不含 maintain/propose）：只回一句，报告收到的 prompt 长度
  * maintain 轮：在 wiki/patterns/ 写一个模式页（FAKE_DELIVER=0 时不写）
  * propose 轮：在 runs/proposals/iter-<N>.json 写一个提案（同上开关）
最后把「原始轨迹」写成 JSONL 到 --session-out 指定路径（模拟 agent 自己的
会话存储），由 backend 归一化。
"""

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    # 与真实 CLI（如 `codex exec -`）一样：可以吃文件，也可以吃 stdin
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--session-out", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-turns", default="")
    a = ap.parse_args()

    if a.prompt_file:
        prompt = open(a.prompt_file, encoding="utf-8").read()
    else:
        prompt = sys.stdin.read()
    deliver = os.environ.get("FAKE_DELIVER", "1") == "1"
    tag = a.tag

    if "maintain" in tag:
        if deliver:
            pat = os.path.join(a.workdir, "wiki", "patterns")
            os.makedirs(pat, exist_ok=True)
            with open(os.path.join(pat, "pattern-fake.md"), "w",
                      encoding="utf-8") as f:
                f.write("# FAKE PATTERN\n\nPROBLEM\nROOT CAUSE\nFIX\n")
            print("wrote wiki/patterns/pattern-fake.md")
        else:
            print("analyzed but wrote nothing")
    elif "propose" in tag:
        if deliver:
            it = tag.split("iter-")[-1].split("-")[0] if "iter-" in tag else "1"
            d = os.path.join(a.workdir, "runs", "proposals")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"iter-{it}.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"action": "create", "name": "fake-skill",
                           "skill_md": "# fake"}, f)
            print(f"wrote runs/proposals/iter-{it}.json")
        else:
            print("analyzed but wrote nothing")

    print(f"prompt_chars={len(prompt)}")
    print(f"model={a.model} max_turns={a.max_turns} tag={tag}")

    if a.session_out:
        os.makedirs(os.path.dirname(a.session_out) or ".", exist_ok=True)
        msgs = [{"role": "user", "content": prompt[:80]},
                {"role": "assistant", "content": "ok",
                 "tool_calls": [{"name": "file_write", "args": {}}]},
                {"role": "tool", "content": "done"}]
        with open(a.session_out, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    if os.environ.get("FAKE_FAIL") == "1":
        print("simulated failure", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
