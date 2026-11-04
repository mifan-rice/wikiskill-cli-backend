"""End-to-end wiring check against the upstream harness — dry run, no model.

Optional: requires the upstream package (`pip install wikiskill`). Exits 0 with
a notice when the package is absent, so it is safe to run anywhere.

    python examples/evolve_dryrun.py

What this proves (and what it does not):

* proves   the upstream orchestrator resolves our backend from the workspace's
           ``workspace.json``, bootstraps the isolated profile per rollout, and
           hands it the documented tags — the whole loop runs end to end;
* does NOT prove model quality: ``dry_run=True`` never launches the CLI, never
           grades, never touches the network (scores are None by design).

The hook count printed at the end is the audit trail: one call per val task
(baseline gate), per train task (rollouts), plus maintainer and proposer.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    from wikiskill import backends, bench, harness, tasks as tasks_mod
except ImportError:                                     # pragma: no cover
    print("wikiskill is not installed - optional check skipped.")
    print("  pip install wikiskill")
    raise SystemExit(0)

from backend import CliBackend  # noqa: E402

FAKE = os.path.join(ROOT, "fixtures", "fake_agent.py")


def main() -> int:
    ws = tempfile.mkdtemp(prefix="wikiskill-evolve-dry-")
    config = {
        "command": [sys.executable, FAKE,
                    "--prompt-file", "{prompt_file}",
                    "--workdir", "{workdir}",
                    "--run-dir", "{run_dir}",
                    "--session-out", "{run_dir}/session.raw.jsonl",
                    "--tag", "{tag}", "--model", "{model}",
                    "--max-turns", "{max_turns}"],
        "profile_dir": ".ws-cli-home",
        "model": "dry-run",
        "session_export": {"mode": "cmd", "cmd": [],
                           "output": "{run_dir}/session.raw.jsonl"},
    }
    backend = CliBackend(config)

    # Register under a workspace name; the harness only knows the registry.
    backends.BACKENDS["cli"] = backend
    backends.write_backend(ws, "cli")

    calls = []
    original_run = backend.run

    def spy_run(ws_, prompt, **kw):
        calls.append({"tag": kw.get("tag"), "dry_run": kw.get("dry_run"),
                      "chars": len(prompt)})
        return original_run(ws_, prompt, **kw)

    backend.run = spy_run

    harness.init_workspace(ws)
    generated = bench.generate(seed=1)
    tasks_mod.save(ws, generated)
    tasks_mod.materialize_all(ws, generated)

    state = harness.evolve(ws, iters=1, dry_run=True, verbose=True)

    print()
    print(f"workspace : {ws}")
    print(f"calls     : {len(calls)}")
    for c in calls:
        print(f"  tag={c['tag']:<28} dry_run={c['dry_run']} "
              f"prompt_chars={c['chars']}")
    profile = os.path.join(ws, backend.profile_dir_name)
    checks = [
        ("backend resolved from workspace.json", len(calls) > 0),
        ("every call was a dry run", all(c["dry_run"] for c in calls)),
        ("isolated profile bootstrapped", os.path.isdir(profile)),
        ("baseline recorded", state.get("baseline") is not None),
        ("prompts were non-empty", all(c["chars"] > 0 for c in calls)),
    ]
    print()
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    print()
    print(f"{len(checks)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
