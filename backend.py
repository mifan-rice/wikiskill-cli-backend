"""wikiskill CLI backend (unofficial).

把 wikiskill 的进化循环接到「任何能一次性跑一个 prompt 的 agent CLI」上，
代替上游内置的 Hermes backend（`hermes chat --oneshot`）。

设计约束（与上游对齐、不侵入上游）：
  * 只实现 `wikiskill.backends.base.AgentBackend` 的协议方法（profile_dir /
    env / bootstrap_profile / set_active_skills / patch_model / run /
    export_session），不 import 上游内部类型、不 patch 其包。
  * RunResult 字段与上游同构自建，可直接作为 runner 注入的返回值。
  * 每次 rollout 用独立 profile 目录（state + skills），gate 才有意义：
    agent 恰好看到候选技能集 S_k，且不共享上一次 rollout 的状态。

与「进程内 SDK」式 backend 的区别：本文件只用 subprocess，不认识任何具体
agent 的内部 API——换 CLI 只改配置，不改代码。

License: MIT（见 LICENSE）。基于论文 arXiv:2608.27454 与
https://github.com/ashutoshsinghpr7/wikiskill 的公开协议独立实现，不含其代码。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, List, Optional

PROFILE_DIR_DEFAULT = ".wikiskill-cli-home"
DEFAULT_MODEL = "default"

#: 这些占位符可出现在 command / env / session_export.output 里
_PLACEHOLDERS = ("ws", "workdir", "run_dir", "tag", "model", "max_turns",
                 "prompt_file", "profile", "state", "skills", "budget",
                 # `prompt` = 全文走 argv（`-z "{prompt}"` 型 CLI）。
                 # ⚠️ argv 有长度上限（Windows 约 32767 字符 / POSIX ARG_MAX），
                 # 大 prompt + 技能全文注入会超；能用 {prompt_file} 就别用这个。
                 "prompt")


class RunResult:
    """与 wikiskill RunResult 字段兼容的轻量结果对象。"""

    def __init__(self, cmd: List[str], exit_code: Optional[int] = None,
                 duration_s: float = 0.0, stdout_path: Optional[str] = None,
                 session_file: Optional[str] = None,
                 dry_run: bool = False, extra: Optional[Dict] = None):
        self.cmd = cmd
        self.exit_code = exit_code
        self.duration_s = duration_s
        self.stdout_path = stdout_path
        self.session_file = session_file
        self.dry_run = dry_run
        self.extra = extra or {}

    def as_dict(self) -> Dict[str, Any]:
        return {"cmd": self.cmd, "exit_code": self.exit_code,
                "duration_s": self.duration_s,
                "stdout_path": self.stdout_path,
                "session_file": self.session_file,
                "dry_run": self.dry_run, **self.extra}


def _force_rmtree(path: str) -> None:
    """删除目录/文件，Windows 上先清只读位（git 对象、缓存文件常见）。"""
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_onerror)
    elif os.path.isfile(path):
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
        except Exception:
            pass


class CliBackend:
    """通用 CLI backend。

    config（dict 或 JSON 文件路径）字段：
        command        : 必填，list[str]，可含占位符（见 _PLACEHOLDERS）
        profile_dir    : profile 目录名（默认 .wikiskill-cli-home）
        env            : 追加/覆盖的子进程环境变量 {name: value}（可含占位符）
        timeout_grace  : 在 run_budget 之上再等的秒数（默认 0）
        keep_state     : True＝run 前不清空隔离 state（默认 False）
        max_deliverable_retries : 交付物缺失时的补交轮上限（默认 0）
        retry_prompt   : 补交提示模板（可含 {missing} {tag}）
        deliverables   : [{"tag_prefix": "maintain", "glob": "wiki/patterns/*.md",
                           "mode": "new_or_changed"|"exists"}]
                         exists 模式的 glob 可用 {iter}（从 tag 的 iter-N 取）
        session_export : {"mode": "cmd"|"stdout"|"none",
                          "cmd": [...], "output": "{run_dir}/session.raw.jsonl"}
        model          : 传给 {model} 的默认值
        max_turns      : 传给 {max_turns} 的默认值
    """

    name = "cli"

    def __init__(self, config: Optional[Any] = None):
        if config is None:
            config = os.environ.get("WIKISKILL_CLI_CONFIG")
        if isinstance(config, str):
            with open(config, encoding="utf-8") as f:
                config = json.load(f)
        self.config: Dict[str, Any] = dict(config or {})
        if not self.config.get("command"):
            raise ValueError("config.command 必填（要跑哪个 agent CLI）")
        self.profile_dir_name = self.config.get("profile_dir",
                                                PROFILE_DIR_DEFAULT)

    # ---------------- profile / 隔离 ----------------

    def profile_dir(self, ws: str) -> str:
        return os.path.join(ws, self.profile_dir_name)

    def _state_dir(self, ws: str) -> str:
        return os.path.join(self.profile_dir(ws), "state")

    def _skills_dir(self, ws: str) -> str:
        return os.path.join(self.profile_dir(ws), "skills")

    def _slots(self, ws: str, **over: Any) -> Dict[str, str]:
        slots = {"ws": ws, "workdir": over.get("workdir") or ws,
                 "profile": self.profile_dir(ws),
                 "state": self._state_dir(ws),
                 "skills": self._skills_dir(ws),
                 "run_dir": over.get("run_dir", ""),
                 "tag": over.get("tag", ""),
                 "model": str(over.get("model", "")),
                 "max_turns": str(over.get("max_turns", "")),
                 "prompt_file": over.get("prompt_file", ""),
                 "prompt": over.get("prompt", ""),
                 "budget": str(over.get("budget", ""))}
        return slots

    def env(self, ws: str) -> dict:
        """子进程环境＝当前环境副本 + config.env。供 CLI 定位自己的隔离 home。"""
        base = dict(os.environ)
        slots = self._slots(ws)
        for k, v in (self.config.get("env") or {}).items():
            base[str(k)] = _expand(str(v), slots)
        return base

    def bootstrap_profile(self, ws: str, real: Optional[str] = None) -> str:
        """建隔离 profile 骨架（state + skills）；不复制任何凭据文件。"""
        prof = self.profile_dir(ws)
        os.makedirs(self._state_dir(ws), exist_ok=True)
        os.makedirs(self._skills_dir(ws), exist_ok=True)
        return prof

    def set_active_skills(self, ws: str, include_framework: bool = False) -> None:
        """重建 profile/skills = workspace skills/active(+framework) 的副本。

        faithful gating 的前提：被测 agent 看到的技能集恰好是 S_k。
        用复制而非 symlink（Windows 无特权时会失败）。
        """
        dst_root = self._skills_dir(ws)
        os.makedirs(dst_root, exist_ok=True)
        for name in os.listdir(dst_root):
            p = os.path.join(dst_root, name)
            if os.path.isdir(p):
                _force_rmtree(p)
            else:
                _force_rmtree(p)
        sources: List[str] = []
        for sub, on in (("active", True), ("framework", include_framework)):
            root = os.path.join(ws, "skills", sub)
            if not on or not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                if name.startswith("."):      # active 常是个 git 仓库
                    continue
                sources.append(os.path.join(root, name))
        for src in sources:
            dst = os.path.join(dst_root, os.path.basename(src))
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

    def patch_model(self, ws: str, model: str, provider: Optional[str] = None) -> None:
        """上游用它改 profile 配置里的默认模型；CLI 场景下只落 marker 备查，
        真正生效靠 command 里的 {model} 占位符。"""
        prof = self.profile_dir(ws)
        os.makedirs(prof, exist_ok=True)
        try:
            with open(os.path.join(prof, "model.txt"), "w",
                      encoding="utf-8") as f:
                f.write(f"{model}\n{provider or ''}\n")
        except OSError:
            pass

    def _reset_state(self, ws: str) -> None:
        """清空隔离 state（oneshot 语义）：残留＝隔离失效，显式失败。"""
        sd = self._state_dir(ws)
        _force_rmtree(sd)
        os.makedirs(sd, exist_ok=True)
        leftover = os.listdir(sd)
        if leftover:
            raise OSError(f"隔离 state 未清空，残留: {leftover}")

    # ---------------- prompt 组装 ----------------

    def _active_skill_texts(self, ws: str,
                            limit_chars: int = 60000) -> List[str]:
        """读 profile/skills 下每个 SKILL.md 全文。

        为什么必须注入：实测模型不会主动去"查技能"，只靠工具自取＝注入失效。
        上游 Hermes 用 `-s` 预加载解决同一问题。
        """
        root = self._skills_dir(ws)
        texts: List[str] = []
        total = 0
        if not os.path.isdir(root):
            return texts
        for name in sorted(os.listdir(root)):
            if name.startswith("."):
                continue
            p = os.path.join(root, name, "SKILL.md")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            total += len(body)
            if total > limit_chars:
                texts.append(f"[skill {name} truncated: over {limit_chars} chars]")
                break
            texts.append(f"[skill {name}]\n{body}")
        return texts

    def _compose_prompt(self, ws: str, prompt: str,
                        include_framework: bool) -> str:
        """推理轮注入 active 技能全文；framework 轮（maintainer/proposer）
        不注入——它们该自己从 wiki/trace 取材，注入反而挤占上下文。"""
        if include_framework:
            return prompt
        texts = self._active_skill_texts(ws)
        if not texts:
            return prompt
        return ("Current skills (full text). Follow them when relevant; "
                "ignore them when irrelevant.\n\n"
                + "\n\n---\n\n".join(texts)
                + "\n\n---\n\n" + prompt)

    # ---------------- 交付物检查（可选、通用） ----------------

    def _rules_for(self, tag: str) -> List[Dict[str, Any]]:
        out = []
        for r in (self.config.get("deliverables") or []):
            pref = r.get("tag_prefix") or ""
            if pref and not (tag.startswith(pref) or f"-{pref}" in f"-{tag}"):
                continue
            out.append(r)
        return out

    def _iter_of(self, tag: str) -> str:
        m = re.search(r"iter-(\d+)", tag or "")
        return m.group(1) if m else ""

    def _expand_glob(self, ws: str, pattern: str, tag: str) -> List[str]:
        pat = pattern.replace("{iter}", self._iter_of(tag))
        full = os.path.join(ws, pat.replace("/", os.sep))
        return sorted(glob.glob(full, recursive=True))

    def _snapshot(self, ws: str, tag: str) -> Dict[str, float]:
        snap: Dict[str, float] = {}
        for r in self._rules_for(tag):
            for p in self._expand_glob(ws, r.get("glob", ""), tag):
                try:
                    snap[p] = os.path.getmtime(p)
                except OSError:
                    pass
        return snap

    def _missing_deliverables(self, ws: str, tag: str,
                              before: Dict[str, float]) -> Optional[str]:
        """有缺 → 返回人类可读的缺失描述；都满足 → None。"""
        for r in self._rules_for(tag):
            mode = r.get("mode", "new_or_changed")
            hits = self._expand_glob(ws, r.get("glob", ""), tag)
            if mode == "exists":
                if not hits:
                    return (f"missing {r.get('glob').replace('{iter}', self._iter_of(tag))}"
                            f" (tag={tag})")
                continue
            fresh = [p for p in hits
                     if p not in before or os.path.getmtime(p) > before[p]]
            if not fresh:
                return f"no new_or_changed file for {r.get('glob')} (tag={tag})"
        return None

    # ---------------- run：subprocess 跑一次 rollout ----------------

    def _cmd_for(self, ws: str, **over: Any) -> List[str]:
        slots = self._slots(ws, **over)
        return [_expand(str(x), slots) for x in self.config["command"]]

    def run(self, ws: str, prompt: str, *, tag: str,
            toolsets: Optional[str] = None, model: Optional[str] = None,
            max_turns: Optional[int] = None, run_budget: int = 300,
            workdir: Optional[str] = None, include_framework: bool = False,
            dry_run: bool = False) -> RunResult:
        """一次 rollout：写 prompt 文件 → 跑 CLI → 收集 stdout/退出码 →
        （可选）交付物检查与补交 → 导出 session。"""
        ws = os.path.abspath(ws)
        model = model or self.config.get("model") or DEFAULT_MODEL
        max_turns = int(max_turns if max_turns is not None
                        else (self.config.get("max_turns") or 15))
        workdir = workdir or ws

        self.set_active_skills(ws, include_framework=include_framework)
        run_dir = os.path.join(ws, "runs", tag)
        os.makedirs(run_dir, exist_ok=True)

        full_prompt = self._compose_prompt(ws, prompt, include_framework)
        prompt_file = os.path.join(run_dir, "query.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(full_prompt)

        cmd = self._cmd_for(ws, workdir=workdir, run_dir=run_dir, tag=tag,
                            model=model, max_turns=max_turns,
                            prompt_file=prompt_file, prompt=full_prompt,
                            budget=run_budget)
        if dry_run:
            return RunResult(cmd=cmd, dry_run=True,
                             extra={"run_dir": run_dir,
                                    "prompt_file": prompt_file,
                                    "prompt_chars": len(full_prompt)})

        timeout = int(run_budget) + int(self.config.get("timeout_grace", 0))
        before = self._snapshot(ws, tag)
        env = self.env(ws)
        t0 = time.time()
        lines: List[str] = []
        exit_code: Optional[int] = None
        attempts = 0
        max_retries = int(self.config.get("max_deliverable_retries", 0))
        retry_tpl = self.config.get("retry_prompt") or (
            "Your previous turn did not deliver the required files. "
            "Deliver now: write the file(s) first, do not analyze further. "
            "Missing: {missing}")

        while True:
            attempts += 1
            try:
                proc = subprocess.run(cmd, cwd=workdir, env=env,
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=timeout,
                                      input=_stdin_payload(self.config,
                                                           prompt_file))
                out, err, code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or "") if isinstance(e.stdout, str) else ""
                err = "TIMEOUT after %ss" % timeout
                code = 124
            except OSError as e:
                out, err, code = "", f"SPAWN ERROR: {type(e).__name__}: {e}", 127
            lines.append(out.rstrip() or "(no stdout)")
            if err.strip():
                lines.append("[stderr]\n" + err.rstrip())
            exit_code = code
            if code != 0:
                break
            missing = self._missing_deliverables(ws, tag, before)
            if missing is None:
                break
            lines.append(f"[deliverable] MISSING: {missing} "
                         f"(retry {attempts}/{max_retries})")
            if attempts > max_retries:
                lines.append("[deliverable] giving up after "
                             f"{attempts - 1} retries")
                break
            retry_prompt = _expand(retry_tpl, {"missing": missing, "tag": tag})
            prompt_file = os.path.join(run_dir,
                                       f"query.retry{attempts}.txt")
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(retry_prompt)
            cmd = self._cmd_for(ws, workdir=workdir, run_dir=run_dir, tag=tag,
                                model=model, max_turns=max_turns,
                                prompt_file=prompt_file, budget=run_budget)

        dur = round(time.time() - t0, 1)
        stdout_path = os.path.join(run_dir, "stdout.txt")
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines))
        session_file = self.export_session(
            ws, os.path.join(run_dir, "session.jsonl"), run_dir=run_dir)
        return RunResult(cmd=cmd, exit_code=exit_code, duration_s=dur,
                         stdout_path=stdout_path, session_file=session_file,
                         extra={"attempts": attempts,
                                "prompt_chars": len(full_prompt)})

    # ---------------- session 导出 ----------------

    def export_session(self, ws: str, dest: str,
                       messages: Optional[List[Dict]] = None,
                       session_id: Optional[str] = None,
                       run_dir: Optional[str] = None) -> Optional[str]:
        """导出 normalized transcript JSONL。

        契约（上游 gating 读首行）：第一行 {tool_call_count, message_count}，
        之后每行一条消息。三种模式：
          cmd    : 跑 config.session_export.cmd 让它吐原始轨迹，再归一化
          stdout : 用本轮 stdout 合成（标记 synthetic，**不要**当成完整轨迹）
          none   : 不导出
        """
        mode = ((self.config.get("session_export") or {}).get("mode")
                or "none")
        if mode == "none":
            return None
        if mode == "cmd":
            slots = self._slots(ws, run_dir=run_dir or "")
            se = self.config["session_export"]
            raw = _expand(str(se.get("output") or
                              os.path.join(dest + ".raw")), slots)
            cmd = [_expand(str(x), slots) for x in se.get("cmd", [])]
            try:
                if cmd: subprocess.run(cmd, cwd=os.path.dirname(raw) or None,
                               env=self.env(ws), capture_output=True,
                               text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if not os.path.isfile(raw):
                return None
            return _normalize_session(raw, dest, synthetic=False)
        # stdout 模式
        out_path = os.path.join(run_dir or "", "stdout.txt")
        text = ""
        if os.path.isfile(out_path):
            try:
                with open(out_path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
        msgs = [{"role": "assistant", "content": text}]
        return _normalize_session(None, dest, synthetic=True, messages=msgs)


def _expand(template: str, slots: Dict[str, str]) -> str:
    """只替换认识的占位符，其余花括号原样保留（避免 CLI 自带 {} 语法炸）。"""
    out = template
    for k in _PLACEHOLDERS:
        out = out.replace("{" + k + "}", str(slots.get(k, "")))
    return out


def _normalize_session(raw: Optional[str], dest: str, *,
                       synthetic: bool,
                       messages: Optional[List[Dict]] = None) -> Optional[str]:
    """把原始轨迹归一化成「首行 meta + 每行一条消息」。"""
    if messages is None:
        messages = []
        if raw and os.path.isfile(raw):
            try:
                with open(raw, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict) and "messages" in obj:
                            messages.extend(obj["messages"] or [])
                        elif isinstance(obj, dict) and obj.get("role"):
                            messages.append(obj)
            except OSError:
                return None
    tool_calls = 0
    for m in messages:
        if m.get("role") == "tool":
            tool_calls += 1
        elif m.get("tool_calls"):
            tool_calls += len(m["tool_calls"] or [])
    meta: Dict[str, Any] = {"tool_call_count": tool_calls,
                            "message_count": len(messages)}
    if synthetic:
        # 明确标记：这是 stdout 合成，不是真实工具轨迹。上游按
        # tool_call_count 做启发式判断时，别让 0 被误当"确实没调工具"。
        meta["synthetic"] = True
        meta["tool_call_count"] = None
    try:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return dest


def _stdin_payload(cfg: Dict[str, Any], prompt_file: str) -> str:
    """子进程 stdin 投喂策略。

    * `stdin: "prompt_file"` → 把 prompt 全文喂进去，给只从 stdin 读的 CLI
      （如 `codex exec -`）用；
    * 未配置 → 喂空串。空串而非 None 是刻意的：None 会让子进程继承父进程
      stdin，`codex exec` 这类 CLI 会一直等 EOF，挂到超时为止。
    """
    if (cfg.get("stdin") or "").strip() != "prompt_file":
        return ""
    try:
        with open(prompt_file, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
