"""One fixed mode contract shared by runtime, permissions, UI and benchmarks."""

from pathlib import Path
from typing import Literal

from backend.observability.inputs import digest

AgentMode = Literal["manual", "accept_edit", "plan", "auto"]
IDENTITY_FIELDS = ("agent_mode", "mode_policy_version", "mode_prompt_version")
READ_TOOLS = frozenset({"ReadFile", "Glob", "Grep", "UseSkill", "ReadSkillResource"})
HARD_BOUNDARIES = [
    "目录必须由用户授权；禁止访问应用内部、密钥与越界资源。",
    "危险命令拦截、版本冲突检查与单次审批绑定不能关闭。",
    "Shell 必须经过操作系统沙箱验证；禁用网络，无沙箱不执行。",
    "文件、技能、记忆及用户补充指令不能提升工具权限。",
    "运行中不能切换模式；每轮固定模式、权限版本与指令版本。",
]
MODES = {
    "manual": {
        "label": "Manual · 手动确认",
        "actions": {"WriteFile": "ask", "EditFile": "ask", "DeleteFile": "ask", "Shell": "ask"},
        "prompt": "读取授权文件以完成用户任务。写入、编辑、删除文件和执行命令须逐项等待用户确认；拒绝后停止，不通过其他工具绕过确认。",
    },
    "accept_edit": {
        "label": "Accept Edit · 自动编辑",
        "actions": {"WriteFile": "allow", "EditFile": "allow", "DeleteFile": "ask", "Shell": "ask"},
        "prompt": "可以直接写入和编辑授权工作区文件。删除文件和执行 Shell 须等待用户确认；不能把需要确认的操作藏在文件编辑里。用实际验证检查实现。",
    },
    "plan": {
        "label": "Plan · 只读规划",
        "actions": {"WriteFile": "deny", "EditFile": "deny", "DeleteFile": "deny", "Shell": "deny"},
        "prompt": "只读取和分析授权资料，输出可供用户审阅的实施计划、涉及文件、风险与验收方法。不修改项目，不执行命令，不声称已经实现或测试通过。用户要求实施时仍只规划，需由用户切换到执行模式后再实施。",
    },
    "auto": {
        "label": "Auto · 沙箱自动执行",
        "actions": {
            "WriteFile": "allow",
            "EditFile": "allow",
            "DeleteFile": "allow",
            "Shell": "allow",
        },
        "prompt": "在用户授权的工作区内自主执行必要的编辑和沙箱命令，完成任务并实际验证。减少无关改动；不能扩展目录授权、访问密钥、联网或绕过固定拒绝规则。",
    },
}
POLICY_VERSION = digest(
    {
        "modes": MODES,
        "read_tools": sorted(READ_TOOLS),
        "boundaries": HARD_BOUNDARIES,
        "guards": {
            name: (Path(__file__).parents[1] / name).read_text()
            for name in (
                "permissions/policy.py",
                "permissions/paths.py",
                "tools/command_guard.py",
                "execution/policy.py",
                "execution/codex.py",
                "runtime/loop.py",
                "runtime/verification.py",
                "tools/file_operations.py",
                "tools/dispatch.py",
                "tools/shell_calls.py",
            )
        },
    }
)


def validate(mode):
    if mode not in MODES:
        raise ValueError("执行模式必须为 manual、accept_edit、plan 或 auto")
    return mode


def tags(value):
    return {key: value.get(key) for key in IDENTITY_FIELDS}


def compatible(case, mode):
    return (case.get("goal", "implementation") == "planning") == (mode == "plan")
