"""Give the Agent task requirements and file locations, never acceptance answers."""

import json


def task_input(case, index):
    files = json.dumps(list(case["files"]), ensure_ascii=False)
    return (
        f"任务初始文件清单（工作区资料）：{files}\n"
        "请先查看已有文件，在对应的现有公开接口中完成需求；不要新建替代函数文件而留下原接口未实现。\n\n"
        + case["prompts"][index]
    )
