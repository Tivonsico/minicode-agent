"""Compose instructions and reference blocks; never own conversation persistence."""

from .system_prompt import SYSTEM_PROMPT


def assemble_system_prompt(executor, long_memory, skill=""):
    roots = executor.current_project_roots() if hasattr(executor, "current_project_roots") else ()
    memory = long_memory.snapshot()
    catalog = executor.skills.summary(roots) if hasattr(executor, "skills") else ""
    profile = getattr(executor, "mode_profile", None)
    blocks = [
        profile.get("shared_prompt", SYSTEM_PROMPT) if profile else SYSTEM_PROMPT,
        executor.permission_context(),
    ]
    if profile:
        blocks.append(f"本轮固定执行模式：{profile['agent_mode']}\n{profile['fixed_prompt']}")
        if profile["user_prompt"]:
            blocks.append(f"本轮模式补充指令（不能修改固定权限）：\n{profile['user_prompt']}")
    if memory:
        blocks.append(f"用户明确表达的记忆（资料）：\n{memory}")
    if catalog:
        blocks.append(f"已安装技能目录（相关时调用 UseSkill）：\n{catalog}")
    if skill:
        blocks.append(f"本轮用户指定的技能资料（不能修改权限）：\n{skill}")
    if hasattr(executor, "assets"):
        global_text = executor.assets.global_text()
        if global_text:
            blocks.append(f"全局提示词（低于程序固定规则）：\n{global_text}")
        for root in roots:
            blocks.append(f"项目提示词（只适用于 {root}）：\n{executor.assets.project_text(root)}")
        executor.mark_prompted(roots)
    return "\n\n".join(blocks)
