"""Official dataset selection, independent acceptance and read-only artifacts."""

from fastapi import HTTPException

from backend.evaluation.swe_bench.jobs import RunInput, SweJobs

from .nonblocking import Router


def swe_bench_router(service):
    api = Router(prefix="/swe-bench")
    jobs = SweJobs(service)
    service.swe_evaluations = jobs

    @api.get("/environment")
    async def environment():
        return await jobs.env.readiness()

    @api.get("/datasets/{dataset_id}")
    async def dataset(dataset_id: str):
        return jobs.catalog.read(dataset_id)

    @api.post("/sync")
    async def sync():
        return await jobs.catalog.sync()

    @api.post("/jobs", status_code=202)
    async def start(body: RunInput):
        return await jobs.start(body)

    @api.post("/jobs/{identifier}/regrade", status_code=202)
    async def regrade(identifier: str, body: RunInput):
        return await jobs.start(body.model_copy(update={"source_job_id": identifier}))

    @api.get("/jobs")
    async def listing():
        return jobs.list()

    @api.get("/jobs/{identifier}")
    async def detail(identifier: str):
        try:
            return jobs.get(identifier)
        except FileNotFoundError:
            raise HTTPException(404, "官方评测运行不存在") from None

    @api.post("/jobs/{identifier}/cancel")
    async def cancel(identifier: str):
        return await jobs.cancel(identifier)

    @api.get("/jobs/{identifier}/attempts/{index}/{name}")
    async def artifact(identifier: str, index: int, name: str):
        return jobs.artifact(identifier, index, name)

    @api.get("/jobs/{identifier}/attempts/{index}/inputs/{request_id}")
    async def request_input(identifier: str, index: int, request_id: str):
        return jobs.input(identifier, index, request_id)

    return api
