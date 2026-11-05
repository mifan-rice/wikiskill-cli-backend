"""端到端冒烟测试：只用夹具假 CLI 验证 CliBackend 协议（不连真实 agent）。

跑法：<python> tests/smoke.py
覆盖：profile 骨架 / 技能集重建(faithful gating) / prompt 注入 / dry_run /
      subprocess 执行与退出码 / 交付物检查与补交 / 失败不吞 / session 归一化。
"""

import json
import os
import shutil
import sys

# 英文 locale 的 Windows runner 默认 cp1252，打印中文用例名会抛 UnicodeEncodeError
# 直接中断整轮测试（表现为 CI 上“1 秒内 exit 1”）。强制 UTF-8 stdio，使本套测试在
# cp936 / cp1252 / UTF-8 主机上行为一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 非 TextIOWrapper 或流已关闭时跳过
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend import CliBackend  # noqa: E402

FAKE = os.path.join(ROOT, "fixtures", "fake_agent.py")
WS = os.path.join(ROOT, ".tmp-ws")
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


def make_ws():
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
        open(p, "w", encoding="utf-8").write(body)


def cfg(**over):
    c = {
        "command": [sys.executable, FAKE,
                    "--prompt-file", "{prompt_file}",
                    "--workdir", "{workdir}",
                    "--run-dir", "{run_dir}",
                    "--session-out", "{run_dir}/session.raw.jsonl",
                    "--tag", "{tag}",
                    "--model", "{model}",
                    "--max-turns", "{max_turns}"],
        "profile_dir": ".ws-cli-home",
        "env": {"FAKE_DELIVER": "1"},
        "deliverables": [
            {"tag_prefix": "maintain", "glob": "wiki/patterns/*.md",
             "mode": "new_or_changed"},
            {"tag_prefix": "propose", "glob": "runs/proposals/iter-{iter}.json",
             "mode": "exists"},
        ],
        "max_deliverable_retries": 1,
        "session_export": {"mode": "cmd",
                           "cmd": [],
                           "output": "{run_dir}/session.raw.jsonl"},
        "model": "fake-model",
        "max_turns": 7,
    }
    c.update(over)
    return c


def main():
    make_ws()
    # 1) profile 骨架 + 技能集重建
    print("[1] profile / faithful gating")
    b = CliBackend(cfg())
    prof = b.bootstrap_profile(WS)
    check("profile dir 建立", os.path.isdir(os.path.join(prof, "state")) and
          os.path.isdir(os.path.join(prof, "skills")), prof)
    b.set_active_skills(WS, include_framework=True)
    got = sorted(os.listdir(os.path.join(prof, "skills")))
    check("active + framework 都在", got == ["demo-skill", "wiki-maintainer"], got)
    b.set_active_skills(WS, include_framework=False)
    got = sorted(os.listdir(os.path.join(prof, "skills")))
    check("framework 轮之外只剩 active", got == ["demo-skill"], got)
    check("重置后 state 为空（oneshot 隔离）",
          os.listdir(os.path.join(prof, "state")) == [])

    # 2) dry_run 不执行
    print("[2] dry_run")
    r = b.run(WS, "hello", tag="iter-1-inference", dry_run=True)
    check("dry_run 标记", r.dry_run is True and r.exit_code is None)
    check("dry_run 展开占位符", any("--prompt-file" == x for x in r.cmd) and
          "fake_agent.py" in " ".join(r.cmd), r.cmd)
    check("dry_run 不产 stdout.txt",
          not os.path.exists(os.path.join(WS, "runs", "iter-1-inference",
                                          "stdout.txt")))

    # 3) 推理轮：技能全文注入
    print("[3] 推理轮 / 技能注入")
    r = b.run(WS, "solve the task", tag="iter-1-inference")
    check("退出码 0", r.exit_code == 0, r.exit_code)
    out = open(r.stdout_path, encoding="utf-8").read()
    n = int([l for l in out.splitlines() if l.startswith("prompt_chars=")][0]
            .split("=")[1])
    check("prompt 被技能全文加长", n > len("solve the task") + 40, n)
    check("model/max_turns 占位符生效", "model=fake-model max_turns=7" in out, out)
    m = json.loads(open(r.session_file, encoding="utf-8").readline())
    check("session 首行 meta 正确",
          m["tool_call_count"] == 2 and m["message_count"] == 3, m)
    check("session 后随 3 条消息",
          len(open(r.session_file, encoding="utf-8").read().strip()
              .splitlines()) == 4)

    # 4) framework 轮：交付物齐 → 不补交
    print("[4] framework 轮 / 交付物满足")
    r = b.run(WS, "maintain the wiki", tag="iter-1-maintain",
              include_framework=True)
    out = open(r.stdout_path, encoding="utf-8").read()
    check("退出码 0", r.exit_code == 0, r.exit_code)
    check("产出模式页", os.path.isfile(os.path.join(WS, "wiki", "patterns",
                                                    "pattern-fake.md")))
    check("无 MISSING 告警", "MISSING" not in out, out)
    check("framework 轮不注入技能全文",
          "prompt_chars" in out and
          int(out.split("prompt_chars=")[1].split()[0]) < 200,
          out.split("prompt_chars=")[-1][:20])

    # 5) 交付物缺失 → 补交一次后放弃（fail-closed 且显式）
    print("[5] 交付物缺失 / 补交上限")
    b2 = CliBackend(cfg(env={"FAKE_DELIVER": "0"}))
    r = b2.run(WS, "propose skill", tag="iter-2-propose",
               include_framework=True)
    out = open(r.stdout_path, encoding="utf-8").read()
    check("记录两次尝试", r.extra.get("attempts") == 2, r.extra)
    check("告警缺失并放弃", "MISSING" in out and "giving up" in out, out)
    check("补交请求落盘",
          os.path.isfile(os.path.join(WS, "runs", "iter-2-propose",
                                      "query.retry1.txt")))

    # 6) CLI 非零退出 → 不吞、不重试
    print("[6] 失败显式上报")
    b3 = CliBackend(cfg(env={"FAKE_DELIVER": "1", "FAKE_FAIL": "1"}))
    r = b3.run(WS, "x", tag="iter-3-inference")
    out = open(r.stdout_path, encoding="utf-8").read()
    check("退出码透传 3", r.exit_code == 3, r.exit_code)
    check("stderr 落 stdout.txt", "simulated failure" in out, out)
    check("非零退出不触发补交", r.extra.get("attempts") == 1, r.extra)

    # 7) session_export=stdout → 合成轨迹必须标记 synthetic
    print("[7] session 合成模式标记")
    b4 = CliBackend(cfg(session_export={"mode": "stdout"}))
    r = b4.run(WS, "y", tag="iter-4-inference")
    m = json.loads(open(r.session_file, encoding="utf-8").readline())
    check("合成轨迹带 synthetic 标记",
          m.get("synthetic") is True and m.get("tool_call_count") is None, m)

    # 8) session_export=none → 明确不导出
    print("[8] 不导出模式")
    b5 = CliBackend(cfg(session_export={"mode": "none"}))
    r = b5.run(WS, "z", tag="iter-5-inference")
    check("session_file=None", r.session_file is None, r.session_file)

    # 9) stdin 投喂（给只吃 stdin 的 CLI，如 `codex exec -`）
    print("[9] stdin 投喂")
    stdin_cmd = [sys.executable, FAKE, "--workdir", "{workdir}",
                 "--run-dir", "{run_dir}",
                 "--session-out", "{run_dir}/session.raw.jsonl",
                 "--tag", "{tag}", "--model", "{model}",
                 "--max-turns", "{max_turns}"]
    b6 = CliBackend(cfg(command=stdin_cmd, stdin="prompt_file"))
    r = b6.run(WS, "prompt via stdin", tag="iter-6-inference")
    out = open(r.stdout_path, encoding="utf-8").read()
    n = int([l for l in out.splitlines() if l.startswith("prompt_chars=")][0]
            .split("=")[1])
    check("stdin 模式退出码 0", r.exit_code == 0, r.exit_code)
    check("prompt 经 stdin 完整到达（含技能全文）",
          n > len("prompt via stdin") + 40, n)
    check("argv 里没有 prompt_file 占位符",
          not any("prompt_file" in x for x in r.cmd), r.cmd)

    # 10) prompt 走 argv（`-z "{prompt}"` 型 CLI，如 hermes --oneshot）
    print("[10] argv prompt 模式")
    argv_cmd = [sys.executable, FAKE, "--prompt", "{prompt}",
                "--tag", "{tag}", "--model", "{model}",
                "--max-turns", "{max_turns}"]
    b7 = CliBackend(cfg(command=argv_cmd))
    r = b7.run(WS, "prompt via argv", tag="iter-7-inference", dry_run=True)
    joined = " ".join(r.cmd)
    check("{prompt} 展开为全文", "prompt via argv" in joined, joined[:200])
    check("argv 模式无 prompt_file 残留", "prompt_file" not in joined,
          joined[:200])
    dr = b7.run(WS, "big prompt " * 3000, tag="iter-7-inference", dry_run=True)
    check("超长 prompt 仍能展开（argv 上限留给调用方判断）",
          len(" ".join(dr.cmd)) > 30000, len(" ".join(dr.cmd)))

    print()
    print(f"检查项 {checks}，失败 {len(fails)}")
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
