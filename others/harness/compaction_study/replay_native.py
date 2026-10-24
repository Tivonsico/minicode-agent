"""Replay the exact ordinary user turns with a larger budget, then ask a held-out recall question."""

import argparse
import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import httpx

from others.harness.compaction_study.live_sessions import events
from others.harness.compaction_study.run import sha, write_json

PROBE = ("请只依据本会话最早那份 Econprior 配置，恢复它关于运行 Python/CLI 命令的 "
         "IMPORTANT 规则：允许的形式是什么？明确禁止的两种形式是什么？请逐字引用。"
         "若当前上下文没有保留，请明确回答‘不知道’，不要根据通用习惯猜测。"
         "不要调用外部工具或访问网络。")


async def connect(client):
    response = await client.get("/")
    response.raise_for_status()
    client.headers["x-minicode-token"] = re.search(
        r'name="minicode-token" content="([^"]+)"', response.text)[1]


async def send(client, runtime, sid, text):
    response = await client.post(f"/api/sessions/{sid}/runs", json={
        "text": text, "reasoning_effort": "low", "agent_mode": "plan"})
    response.raise_for_status()
    run_id = response.json()["run_id"]
    started = time.monotonic()
    while time.monotonic() - started < 180:
        journal = events(runtime, sid)
        ended = [e for e in journal if e["type"] == "run_end" and e.get("run_id") == run_id]
        if ended:
            response = await client.get(f"/api/sessions/{sid}")
            response.raise_for_status()
            if not response.json()["running"]:
                final = [e for e in journal if e["type"] == "message" and e.get("run_id") == run_id
                         and e.get("is_final")]
                return {"run_id": run_id, "status": ended[-1]["status"], "prompt": text,
                        "response": final[-1]["content"] if final else None,
                        "native_compactions": sum(e["type"] == "context_compressed" for e in journal),
                        "elapsed_seconds": time.monotonic() - started}
        await asyncio.sleep(0.5)
    await client.post(f"/api/sessions/{sid}/cancel")
    raise TimeoutError("Turn timeout; conversation saved")


async def main(args):
    source, output = Path(args.source), Path(args.output)
    campaign = json.loads((source / "campaign.json").read_text())
    if campaign["status"] != "completed":
        raise ValueError("Baseline campaign must finish before the held-out comparison")
    output.mkdir(parents=True, exist_ok=False)
    record = {"protocol": "Same frozen user turns via normal HTTP API; all original task inputs "
                          "are preserved in the control history. No manually injected assistant messages.",
              "source_campaign_sha256": sha(source / "campaign.json"),
              "probe": PROBE, "replay": [], "status": "running"}
    try:
        async with httpx.AsyncClient(base_url=campaign["url"], timeout=30) as native:
            await connect(native)
            record["native_probe"] = await send(native, Path(campaign["runtime_root"]),
                                                 campaign["session_id"], PROBE)
            print("NATIVE PROBE:", record["native_probe"]["response"], flush=True)
            write_json(output / "comparison.json", record)
        async with httpx.AsyncClient(base_url=args.control_url, timeout=30) as control:
            await connect(control)
            response = await control.post("/api/sessions")
            response.raise_for_status()
            detail = response.json()
            sid = detail["session"]["session_id"]
            record.update(control_session=sid, control_url=args.control_url,
                          control_input_cap=detail["context_budget"])
            for index, turn in enumerate(campaign["turns"]):
                result = await send(control, Path(args.control_runtime), sid, turn["prompt"])
                record["replay"].append(result)
                write_json(output / "comparison.json", record)
                print(f"CONTROL {index + 1}/{len(campaign['turns'])}: {result['status']}; "
                      f"compactions={result['native_compactions']}", flush=True)
                if result["status"] != "completed":
                    raise RuntimeError("Control turn failed; do not report an accuracy comparison")
            record["control_probe"] = await send(control, Path(args.control_runtime), sid, PROBE)
            print("CONTROL PROBE:", record["control_probe"]["response"], flush=True)
        record["status"] = "completed"
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        for label, runtime, sid in [
            ("native", Path(campaign["runtime_root"]), campaign["session_id"]),
            ("control", Path(args.control_runtime), record.get("control_session")),
        ]:
            if sid:
                shutil.copytree(runtime / ".minicode/sessions" / sid,
                                output / label, dirs_exist_ok=True)
        write_json(output / "comparison.json", record)
        write_json(output / "evidence-sha256.json", {
            str(p.relative_to(output)): sha(p) for folder in ["native", "control"]
            for p in sorted((output / folder).rglob("*")) if p.is_file()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--control-url", default="http://127.0.0.1:8878")
    parser.add_argument("--control-runtime", required=True)
    asyncio.run(main(parser.parse_args()))
