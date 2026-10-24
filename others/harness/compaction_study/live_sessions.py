"""Run author AAC compression tasks through ordinary MiniCode HTTP conversations.

One live request at a time. No replacement provider, fake messages, manual
compaction, or character truncation. Grade actual context_compressed events.
"""

import argparse
import asyncio
import json
import random
import re
import shutil
import time
from pathlib import Path

import httpx

from others.harness.compaction_study.paper_run import COMMIT, upstream_modules
from others.harness.compaction_study.run import sha, write_json


def events(runtime, sid):
    path = runtime / ".minicode/sessions" / sid / "session.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            break  # A concurrent writer may not have flushed the final line yet.
    return rows


def grade_compactions(journal, configs, preservation):
    result = []
    for index, event in enumerate(e for e in journal if e["type"] == "context_compressed"):
        text = "\n".join(str(m.get("content", "")) for m in event["prepared_history"])
        summary_text = event["prepared_history"][0].get("content", "")
        seen = [c for c in configs if c.get("first_seq", 10**12) <= event["seq"]]
        details = []
        for config in seen:
            rules = config["constraints"]
            checks = [{"constraint": rule,
                       "prepared_preserved": preservation.constraint_preserved(rule, text),
                       "summary_preserved": preservation.constraint_preserved(rule, summary_text)}
                      for rule in rules]
            details.append({"config": config["name"], "checks": checks,
                            "prepared_recall": sum(c["prepared_preserved"] for c in checks) / len(checks),
                            "summary_recall": sum(c["summary_preserved"] for c in checks) / len(checks)})
        result.append({"compression": index + 1, "seq": event["seq"],
                       "source_digest": event["source_digest"],
                       "input_estimate": event["input_estimate"],
                       "details": details,
                       "oldest_config_recall": details[0]["prepared_recall"] if details else None,
                       "macro_prepared_recall": sum(d["prepared_recall"] for d in details) / len(details)
                       if details else None})
    return result


async def main(args):
    output, runtime, upstream = map(Path, [args.output, args.runtime_root, args.upstream])
    if output.exists():
        raise ValueError("Use a new output directory; never implicitly replay a conversation")
    import subprocess
    commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if commit != COMMIT:
        raise ValueError("Upstream commit mismatch")
    if subprocess.run(["git", "-C", str(upstream), "diff", "--quiet", COMMIT, "--",
                       "knowledge_triage", "experiments/_helpers.py",
                       "experiments/multiturn_stability.py", "data/sample"]).returncode:
        raise ValueError("Upstream sources differ from pinned commit")
    classifier, _, _, preservation, helpers, prompt = upstream_modules(upstream)
    candidates = sorted((upstream / "data/sample").glob("*.md"))
    random.Random(11).shuffle(candidates)
    configs = []
    for path in candidates:
        kb = helpers["build_kb_e2"](path)
        if not kb or not 800 <= len(path.read_text()) <= 6000:
            continue
        rules = [i.text for i in kb.by_type(classifier.KnowledgeType.CONSTRAINT)]
        if not rules:
            continue
        published = subprocess.check_output([
            "git", "-C", str(upstream), "show", f"{COMMIT}:data/sample/{path.name}"])
        if published != path.read_bytes():
            raise ValueError("Sample differs from published upstream data")
        configs.append({"name": path.name, "text": helpers["_kb_text"](kb), "constraints": rules,
                        "initial_proxy_tokens": kb.total_tokens, "sha256": sha(path)})
        if len(configs) == args.max_configs:
            break
    output.mkdir(parents=True)
    for config in configs:
        target = output / "data" / config["name"]
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(upstream / "data/sample" / config["name"], target)
    shutil.copyfile(upstream / "LICENSE", output / "data/UPSTREAM-LICENSE")
    shutil.copyfile(upstream / "data/sample/README.md", output / "data/UPSTREAM-README.md")
    from others.harness.compaction_study.native_report import archive_sources
    archive_sources(output, upstream)
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        page = await client.get("/")
        page.raise_for_status()
        token = re.search(r'name="minicode-token" content="([^"]+)"', page.text)[1]
        client.headers["x-minicode-token"] = token
        response = await client.get("/api/info")
        response.raise_for_status()
        info = response.json()
        response = await client.post("/api/sessions")
        response.raise_for_status()
        session = response.json()
        sid = session["session"]["session_id"]
        record = {"kind": "normal_minicode_conversation", "session_id": sid,
                  "runtime_root": str(runtime.resolve()), "url": args.url,
                  "paper": "2608.22752 §4.3 / Fig.3", "upstream_commit": COMMIT,
                  "model_info": info, "concurrency": 1, "rounds_per_config": 5,
                  "long_memory": args.long_memory,
                  "context_budget": session["context_budget"],
                  "model_capabilities": session["model_capabilities"],
                  "minimum_compactions": args.min_compactions, "max_configs": args.max_configs,
                  "stop_rule": "Finish current five-round config, then stop at minimum native "
                               "compactions; otherwise continue preselected configs up to max.",
                  "classification": "author regex; original cascade labels unavailable",
                  "protocol_difference": "Ordinary conversation retains earlier messages; native "
                     "history compaction is automatic. No author post-hoc character truncation. "
                     "Paper prompt output limits are requests, and overshoots are reported.",
                  "configs": configs, "turns": [], "status": "running"}
        write_json(output / "campaign.json", record)
        print(f"NORMAL SESSION {sid} at {args.url}", flush=True)
        try:
            for ci, config in enumerate(configs):
                current = config["text"]
                budget = max(64, int(config["initial_proxy_tokens"] * 0.5))
                for r in range(1, 6):
                    # Original author task prompt and recursive previous *actual*
                    # reply. The application alone decides native history compaction.
                    text = prompt.format(target_tokens=budget, config_text=current[:30000])
                    before = events(runtime, sid)
                    before_seq = before[-1]["seq"] if before else 0
                    config.setdefault("first_seq", before_seq + 1)
                    started = time.perf_counter()
                    response = await client.post(f"/api/sessions/{sid}/runs", json={
                        "text": text, "reasoning_effort": "low", "agent_mode": "plan"})
                    response.raise_for_status()
                    run_id = response.json()["run_id"]
                    deadline = time.monotonic() + args.turn_timeout
                    while True:
                        journal = events(runtime, sid)
                        ended = [e for e in journal if e["type"] == "run_end" and e.get("run_id") == run_id]
                        if ended:
                            detail = await client.get(f"/api/sessions/{sid}")
                            detail.raise_for_status()
                            if not detail.json()["running"]:
                                break
                        if time.monotonic() > deadline:
                            await client.post(f"/api/sessions/{sid}/cancel")
                            raise TimeoutError("Ordinary conversation turn timed out; preserved")
                        await asyncio.sleep(0.5)
                    messages = [e for e in journal if e["type"] == "message"
                                and e.get("run_id") == run_id and e.get("is_final")]
                    if not messages or ended[-1]["status"] != "completed":
                        raise RuntimeError("Ordinary turn did not complete; inspect raw session")
                    current = messages[-1]["content"]
                    checks = [{"constraint": c, "preserved": preservation.constraint_preserved(c, current)}
                              for c in config["constraints"]]
                    compactions = grade_compactions(journal, configs[:ci + 1], preservation)
                    turn = {"config": config["name"], "round": r, "run_id": run_id,
                            "first_seq": before_seq + 1, "last_seq": journal[-1]["seq"],
                            "prompt": text, "response": current, "budget_proxy_tokens": budget,
                            "output_proxy_tokens": len(current) // 4,
                            "proxy_budget_met": len(current) // 4 <= budget,
                            "constraint_recall": sum(c["preserved"] for c in checks) / len(checks),
                            "checks": checks, "native_compactions_so_far": len(compactions),
                            "elapsed_ms": (time.perf_counter() - started) * 1000}
                    record["turns"].append(turn)
                    record["native_compactions"] = compactions
                    write_json(output / "campaign.json", record)
                    print(f"{config['name']} r{r} recall={turn['constraint_recall']:.3f} "
                          f"NATIVE_COMPACTIONS={len(compactions)}", flush=True)
                    budget = max(64, int(len(current) // 4 * 0.5))
                if len(compactions) >= args.min_compactions:
                    record["status"] = "completed"
                    break
            else:
                record["status"] = "insufficient_native_compactions"
        except BaseException as exc:
            record.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                          error_type=type(exc).__name__)
            raise
        finally:
            source = runtime / ".minicode/sessions" / sid
            shutil.copytree(source, output / "session", dirs_exist_ok=True)
            write_json(output / "campaign.json", record)
            write_json(output / "evidence-sha256.json", {
                str(p.relative_to(output)): sha(p) for p in sorted((output / "session").rglob("*"))
                if p.is_file()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8876")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--upstream", default="/tmp/minicode-compaction-upstream-20261022")
    parser.add_argument("--min-compactions", type=int, default=5)
    parser.add_argument("--max-configs", type=int, default=20)
    parser.add_argument("--turn-timeout", type=int, default=180)
    parser.add_argument("--long-memory", choices=["enabled", "disabled"], default="disabled")
    asyncio.run(main(parser.parse_args()))
