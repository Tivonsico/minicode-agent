"""Coding benchmark jobs and retrospective evaluation screen API."""

from fastapi import HTTPException, Query

from backend.evaluation.jobs import BenchmarkJobs
from backend.evaluation.models import BenchmarkInput, DatasetInput, EvaluationInput, ImportInput
from backend.evaluation.regrade import regrade
from backend.evaluation.repository import EvaluationRepository
from backend.evaluation.runner import evaluate

from .nonblocking import Router


def evaluation_router(service, session):
    api = Router(prefix="/evaluations")
    repository = EvaluationRepository(service.config.root)
    jobs = BenchmarkJobs(service, repository)
    service.evaluations = jobs

    @api.get("/dataset")
    async def dataset():
        return repository.dataset()

    @api.put("/dataset")
    async def save_dataset(body: DatasetInput):
        return repository.save(body)

    @api.post("/imports", status_code=201)
    async def import_session(body: ImportInput):
        return repository.import_session(session(body.session_id), body.revision)

    @api.post("/jobs", status_code=202)
    async def start_benchmark(body: BenchmarkInput):
        return jobs.start(body)

    @api.get("/jobs")
    async def benchmark_jobs():
        return jobs.list()

    @api.get("/jobs/{identifier}")
    async def benchmark_job(identifier: str):
        try:
            return jobs.get(identifier)
        except FileNotFoundError:
            raise HTTPException(404, "评测运行不存在") from None

    @api.post("/jobs/{identifier}/cancel")
    async def cancel_benchmark(identifier: str):
        return await jobs.cancel(identifier)

    @api.post("/jobs/{identifier}/regrade", status_code=201)
    async def regrade_job(identifier: str):
        return await regrade(jobs, identifier)

    @api.get("/jobs/{identifier}/attempts/{index}")
    async def benchmark_artifact(identifier: str, index: int):
        return jobs.artifact(identifier, index)

    @api.get("/jobs/{identifier}/attempts/{index}/input")
    async def benchmark_input(identifier: str, index: int, ref: str = Query()):
        return jobs.input(identifier, index, ref)

    @api.post("/runs", status_code=201)
    async def run(body: EvaluationInput):
        items = [session(sid) for sid in dict.fromkeys(body.session_ids)]
        if any(item.busy() for item in items):
            raise HTTPException(409, "所选会话仍在执行，请结束后评测以固定样本")
        selected = repository.dataset()
        if body.case_ids is not None:
            identifiers = set(body.case_ids)
            cases = [case for case in selected["cases"] if case["id"] in identifiers]
            if len(cases) != len(identifiers) or any(case["mode"] != "history" for case in cases):
                raise HTTPException(422, "请选择存在的会话检查规则")
            selected = {**selected, "cases": cases}
        return repository.save_report(evaluate(selected, items))

    @api.get("/reports")
    async def reports():
        return repository.reports()

    @api.get("/reports/{identifier}")
    async def report(identifier: str):
        try:
            return repository.report(identifier)
        except FileNotFoundError:
            raise HTTPException(404, "评测报告不存在") from None

    return api
