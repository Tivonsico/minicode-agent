"""Re-ask the held-out question in the same persisted, compressed session after recall is added."""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

import httpx

from others.harness.compaction_study.live_sessions import events
from others.harness.compaction_study.replay_native import PROBE, connect, send
from others.harness.compaction_study.run import sha, write_json


async def main(args):
    source, output = Path(args.source), Path(args.output)
    campaign = json.loads((source / "campaign.json").read_text())
    runtime, sid = Path(campaign["runtime_root"]), campaign["session_id"]
    output.mkdir(parents=True, exist_ok=False)
    before = events(runtime, sid)
    cutoff = before[-1]["seq"]
    record = {"protocol": "Same compressed session, original journal and input cap; no manual "
                          "source reinjection. Re-ask identical question with experimental "
                          "bounded program recall and read-only RecallHistory enabled. "
                          "Includes a process restart and the earlier failed answer: a targeted "
                          "recovery demonstration, not randomized or independent task-success rate.",
              "session_id": sid, "before_seq": cutoff, "probe": PROBE,
              "prior_journal_sha256": sha(runtime / ".minicode/sessions" / sid / "session.jsonl"),
              "source_sha256": {str(p): sha(p) for p in [
                  Path("backend/tools/history_calls.py"), Path("backend/tools/registry.py"),
                  Path("backend/tools/executor.py"),
                  Path("backend/tools/dispatch.py"), Path("backend/modes/catalog.py"),
                  Path("backend/context/system_prompt.py"), Path("backend/context/history_recall.py"),
                  Path("backend/context/prompts.py"), Path("backend/config.py"),
                  Path("backend/runtime/session.py")]}}
    try:
        async with httpx.AsyncClient(base_url=campaign["url"], timeout=30) as client:
            await connect(client)
            response = await client.get(f"/api/sessions/{sid}")
            response.raise_for_status()
            record["context_budget"] = response.json()["context_budget"]
            record["result"] = await send(client, runtime, sid, PROBE)
            after = events(runtime, sid)
            record["new_events"] = [e for e in after if e["seq"] > cutoff]
            print(record["result"]["response"], flush=True)
    finally:
        shutil.copytree(runtime / ".minicode/sessions" / sid, output / "session", dirs_exist_ok=True)
        for relative in record["source_sha256"]:
            path = Path(relative)
            destination = output / "source" / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        write_json(output / "result.json", record)
        write_json(output / "evidence-sha256.json", {
            str(p.relative_to(output)): sha(p) for p in sorted((output / "session").rglob("*"))
            if p.is_file()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
