"""Official APIs run in their own interpreter, never in the Agent workspace.

Expected patches and test scripts are only loaded here. The application receives
an allowlist of public issue fields, or an official grading report.
"""

import contextlib
import importlib.metadata
import json
import sys
import tarfile
import uuid
from pathlib import Path


def catalog(repository):
    from swebench.task.repo import published_tasks

    fields = ("instance_id", "repo", "base_commit", "problem_statement", "version")
    datasets = ["SWE-bench/SWE-bench_Lite", "SWE-bench/SWE-bench_Verified"]
    return {
        name: [{key: task[key] for key in fields} for task in splits.get("test", [])]
        for name, splits in published_tasks(repository, datasets).items()
    }


def ready():
    import docker

    result = {"version": importlib.metadata.version("swebench"), "docker_ready": False}
    client = None
    try:
        client = docker.from_env(timeout=5)
        info = client.info()
        result.update(
            docker_ready=True,
            docker_version=info["ServerVersion"],
            docker_arch=info["Architecture"],
            docker_os=info["OSType"],
        )
    except docker.errors.DockerException as exc:
        result["reason"] = str(exc)
    finally:
        if client:
            client.close()
    return result


def official_image(client, name):
    """Published task images are amd64 even when the Docker engine is ARM."""
    import docker

    try:
        image = client.images.get(name)
    except docker.errors.ImageNotFound:
        print("Pulling official task image: " + name, flush=True)
        image = client.images.pull(name, platform="linux/amd64")
    if image.attrs.get("Architecture") != "amd64":
        raise ValueError("官方任务镜像不是 linux/amd64，未执行验收")
    return image


def record_image(image, name):
    Path("image.json").write_text(
        json.dumps(
            {
                "requested_image": name,
                "image_id": image.id,
                "repo_digests": image.attrs.get("RepoDigests", []),
                "architecture": image.attrs.get("Architecture"),
            }
        )
    )


def grade(repository, instance_id, run_id, timeout):
    import docker
    from swebench.harness.run_evaluation import main
    from swebench.task.repo import load_task_repo

    tasks = load_task_repo(repository, [instance_id])
    with contextlib.closing(docker.from_env(timeout=1800)) as client:
        record_image(official_image(client, tasks[0]["image"]), tasks[0]["image"])
    # A local official dataset is a supported harness input. Published images
    # avoid rebuilding conda and the entire repository on every evaluation.
    Path("official-dataset.json").write_text(json.dumps(tasks), encoding="utf-8")
    main(
        dataset_name=str(Path("official-dataset.json").resolve()),
        split="test",
        instance_ids=[instance_id],
        predictions_path="predictions.jsonl",
        max_workers=1,
        open_file_limit=4096,
        run_id=run_id,
        timeout=int(timeout),
        rewrite_reports=False,
        modal=False,
        report_dir="official",
        task_repo=None,
    )


def prepare(repository, instance_id, cache, run_id):
    """Extract a pristine source baseline from the official published image."""
    import docker
    from swebench.task.repo import load_task_repo

    task = load_task_repo(repository, [instance_id])[0]
    cache = Path(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    staging = cache.with_name("extract-" + uuid.uuid4().hex)
    staging.mkdir()
    container = None
    archive = staging / "repository.tar"
    try:
        with contextlib.closing(docker.from_env(timeout=1800)) as client:
            image = official_image(client, task["image"])
            container = client.containers.create(
                image.id, command="true", network_disabled=True, name="sweb.eval.prepare." + run_id
            )
            stream, _ = container.get_archive("/testbed")
            with archive.open("wb") as output:
                for chunk in stream:
                    output.write(chunk)
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            (staging / "testbed").rename(cache)
            record_image(image, task["image"])
    finally:
        if container:
            container.remove(force=True)
        import shutil

        shutil.rmtree(staging)


def cleanup(run_id):
    """Only this UUID's containers; never prune user images or other jobs."""
    import docker

    with contextlib.closing(docker.from_env(timeout=10)) as client:
        for container in client.containers.list(all=True):
            if container.name.startswith("sweb.eval.") and container.name.endswith("." + run_id):
                container.remove(force=True)
    return {"cleaned": True}


def main():
    action, *args = sys.argv[1:]
    if action in {"grade", "prepare"}:
        {"grade": grade, "prepare": prepare}[action](*args)
        return
    with contextlib.redirect_stdout(sys.stderr):
        value = {"ready": ready, "catalog": catalog, "cleanup": cleanup}[action](*args)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
