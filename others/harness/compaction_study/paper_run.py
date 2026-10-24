"""Public-sample METHOD reproduction; original prompts/operators/lexical scorer.

Replaces unavailable cascade labels with the author's regex classifier, and
unavailable paper models with the configured model. These are material differences.
"""

import argparse
import ast
import asyncio
import importlib
import json
import pathlib
import random
import shutil
import sys
import types
from dataclasses import replace
from pathlib import Path

import httpx

from backend.config import load_config
from others.harness.compaction_study.run import (
    RecordedProvider,
    seal_evidence,
    sha,
    verify_evidence,
    write_json,
)

COMMIT = "a6ceb01a3368cee25ef7ebcf05ebdab8c9be24a4"


def upstream_modules(root):
    # Bypass only the upstream convenience __init__ importing unavailable embedding
    # services. The four modules themselves execute unchanged from the pinned source.
    package = types.ModuleType("knowledge_triage")
    package.__path__ = [str(root / "knowledge_triage")]
    sys.modules["knowledge_triage"] = package
    classifier = importlib.import_module("knowledge_triage.classifier")
    kb = importlib.import_module("knowledge_triage.kb")
    operators = importlib.import_module("knowledge_triage.operators")
    preservation = importlib.import_module("knowledge_triage.preservation")
    # Execute precisely the author's preprocessing functions, without unrelated
    # client imports and cache-directory creation in its experiment entry point.
    tree = ast.parse((root / "experiments/_helpers.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {"build_kb_e2", "_kb_text"}]
    import re
    namespace = {"re": re, "pathlib": pathlib, "Item": kb.Item, "KnowledgeBase": kb.KnowledgeBase,
                 "classify_instruction": classifier.classify_instruction}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "upstream_helpers", "exec"), namespace)
    tree = ast.parse((root / "experiments/multiturn_stability.py").read_text())
    prompt = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "VANILLA_PROMPT" for t in n.targets))
    return classifier, kb, operators, preservation, namespace, prompt


def recompute(output):
    verify_evidence(output)
    _, _, _, preservation, _, _ = upstream_modules(output / "upstream")
    rows = []
    for path in sorted((output / "raw").glob("*/*/round-*.json")):
        row = json.loads(path.read_text())
        if row["status"] == "completed":
            calls = [json.loads(p.read_text()) for p in
                     sorted((path.parent / f"round-{row['round']}-calls").glob("call-*.json"))]
            row["api_calls"] = len(calls)
            row["api_elapsed_ms"] = sum(c.get("elapsed_ms", 0) for c in calls)
            row["unknown_usage_calls"] = sum(not c.get("response", {}).get("usage") for c in calls)
            row["known_total_tokens"] = sum(c.get("response", {}).get("usage", {}).get("total_tokens", 0)
                                            for c in calls)
            row["summary_chars"] = len(row["visible_text"])
            row["details"] = [{"constraint": c,
                               "preserved": preservation.constraint_preserved(c, row["visible_text"])}
                              for c in row["original_constraints"]]
            row["c_recall"] = sum(d["preserved"] for d in row["details"]) / len(row["details"])
        rows.append(row)
    aggregates = []
    for arm in ["vanilla", "minicode_original", "typecompact_author_state", "typecompact_visible_state"]:
        for r in range(1, 6):
            selected = [v for v in rows if v["arm"] == arm and v["round"] == r
                        and v["status"] == "completed"]
            aggregates.append({"arm": arm, "round": r, "n": len(selected),
                               "mean_c_recall": sum(v["c_recall"] for v in selected) / len(selected)
                               if selected else None,
                               "unsafe": sum(v.get("operator_status") == "COMPACTION_UNSAFE"
                                             for v in selected)})
    write_json(output / "scores.json", {"rows": rows, "by_arm_round": aggregates})


async def main(args):
    output = Path(args.output)
    if args.rescore:
        recompute(output)
        return
    root = Path(args.upstream)
    import subprocess
    actual_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual_commit != COMMIT:
        raise ValueError("Upstream commit mismatch")
    checked_paths = ["knowledge_triage/classifier.py", "knowledge_triage/kb.py",
                     "knowledge_triage/operators.py", "knowledge_triage/preservation.py",
                     "experiments/_helpers.py", "experiments/multiturn_stability.py", "data/sample"]
    if subprocess.run(["git", "-C", str(root), "diff", "--quiet", COMMIT, "--", *checked_paths]).returncode:
        raise ValueError("Upstream working files differ from pinned commit")
    output.mkdir(parents=True, exist_ok=False)
    for name in ["classifier.py", "kb.py", "operators.py", "preservation.py"]:
        target = output / "upstream/knowledge_triage" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "knowledge_triage" / name, target)
    for name in ["_helpers.py", "multiturn_stability.py"]:
        target = output / "upstream/experiments" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "experiments" / name, target)
    shutil.copyfile(root / "LICENSE", output / "upstream/LICENSE")
    classifier, kb_module, operators, preservation, helpers, prompt = upstream_modules(root)
    files = sorted((root / "data/sample").glob("*.md"))
    random.Random(11).shuffle(files)
    selected = []
    for path in files:
        kb = helpers["build_kb_e2"](path)
        if kb and kb.by_type(classifier.KnowledgeType.CONSTRAINT):
            selected.append((path, kb))
            if len(selected) == args.n:
                break
    config = replace(load_config(), max_output_tokens=4096)
    # Deliberately keep the same validated GLM adapter contract as the pilot.
    if not config.model.casefold().startswith("glm-"):
        raise ValueError("Requires configured GLM model")
    manifest = {"upstream_commit": COMMIT, "model": config.model, "n": len(selected),
                "seed": 11, "rounds": 5, "ratio": 0.5, "minimum_proxy_budget": 64,
                "labeler": "author regex classifier, NOT original cascade classifier/cache",
                "selection": "sorted public sample paths, shuffled; first eligible n",
                "prompt": prompt, "source_sha256": sha(Path(__file__)),
                "files": {p.name: sha(p) for p, _ in selected},
                "limitations": "method reproduction; no original 20 sample IDs or labels/models"}
    write_json(output / "manifest.json", manifest)
    for path, _ in selected:
        target = output / "inputs" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(root / "data/sample/README.md", output / "inputs/UPSTREAM-README.md")
    shutil.copyfile(root / "data/sample/manifest.json", output / "inputs/UPSTREAM-manifest.json")
    write_json(output / "inputs-integrity.json",
               {str(p.relative_to(output)): sha(p) for folder in ["inputs", "upstream"]
                for p in sorted((output / folder).rglob("*")) if p.is_file()})
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=120) as client:
        async def run(path, original, arm):
            original_constraints = [i.text for i in original.by_type(classifier.KnowledgeType.CONSTRAINT)]
            current_kb = original
            current_text = helpers["_kb_text"](original)
            budget = max(64, int(original.total_tokens * 0.5))
            for r in range(1, 6):
                directory = output / "raw" / path.stem / arm
                row = {"config": path.name, "arm": arm, "round": r, "budget_proxy_tokens": budget,
                       "input_text": current_text, "original_constraints": original_constraints,
                       "status": "started"}
                try:
                    if arm in {"vanilla", "minicode_original"}:
                        provider = RecordedProvider(config, directory / f"round-{r}-calls", client, semaphore)
                        if arm == "vanilla":
                            response = await provider.complete(
                                [{"role": "user", "content": prompt.format(
                                    target_tokens=budget, config_text=current_text[:30000])}], [])
                            if response.finish_reason != "stop":
                                raise ValueError("Incomplete response")
                            text = response.content
                        else:
                            from backend.context.compactor import summarize
                            from others.harness.compaction_study.run import Store
                            text = await summarize(provider, Store(),
                                                   [{"role": "user", "content": current_text}], 100000)
                        row["raw_output"] = text
                        current_text = text[:budget * 4]
                        next_budget = max(64, int(len(current_text) // 4 * 0.5))
                    else:
                        next_kb, status = operators.type_compact(current_kb, budget)
                        text = helpers["_kb_text"](next_kb)
                        current_text = text[:budget * 4]
                        row.update(operator_status=status, full_output=text,
                                   hidden_characters=max(0, len(text) - len(current_text)))
                        if arm == "typecompact_author_state":
                            current_kb = next_kb
                        else:
                            current_kb = kb_module.KnowledgeBase.from_lines(current_text.splitlines())
                        next_budget = max(64, int(current_kb.total_tokens * 0.5))
                    row.update(visible_text=current_text, status="completed")
                    row["details"] = [{"constraint": c,
                                       "preserved": preservation.constraint_preserved(c, current_text)}
                                      for c in original_constraints]
                    row["c_recall"] = sum(d["preserved"] for d in row["details"]) / len(original_constraints)
                    print(f"paper {path.name} {arm} r{r}: {row['c_recall']:.3f}", flush=True)
                    budget = next_budget
                except Exception as exc:
                    row.update(status="failed", error_type=type(exc).__name__)
                    write_json(directory / f"round-{r}.json", row)
                    return
                write_json(directory / f"round-{r}.json", row)
        await asyncio.gather(*(run(path, kb, arm) for path, kb in selected for arm in
                               ["vanilla", "minicode_original", "typecompact_author_state", "typecompact_visible_state"]))
    seal_evidence(output)
    recompute(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="/tmp/minicode-compaction-upstream-20261022")
    parser.add_argument("--output", required=True)
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--rescore", action="store_true")
    asyncio.run(main(parser.parse_args()))
