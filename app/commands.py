COMMANDS = ["/session", "/new", "/allow-dir", "/deny-dir", "/work-dir", "/dirs", "/skill", "/use", "/see", "/tools", "/memory", "/help", "/exit"]
HELP = """/session           选择会话（↑↓选择，回车加载，Esc 关闭列表）
/new               新会话
/allow-dir 路径     为当前会话添加一个本地绝对路径
/deny-dir 路径      撤销当前会话的额外目录授权
/work-dir 路径      切换默认工作目录（已授权）
/dirs              浏览授权目录；回车切换默认目录，D 撤销
/skill             浏览可用技能目录；回车选择技能
/skill 名称        指定下一轮必须使用的技能，之后恢复自动选择
/use global skills [名称]   浏览或指定全局技能
/use project skills [名称]  浏览或指定项目技能；名称直达使用默认工作目录
/see system_prompt         浏览全局和已授权项目的提示词
F1                编辑所选技能或提示词；Enter 保存，Ctrl+J 换行
F2                返回；编辑时取消未保存的改动
/tools             浏览工具调用；回车展开或收起详情
Ctrl+T            展开或收起最近一次工具调用
/memory            查看长期记忆
/memory delete ID  删除指定记忆
/help              显示帮助
/exit              退出
Esc                中断当前轮并保存
Ctrl+Y / Ctrl+N    批准 / 拒绝正在等待的工具请求
空格               展开或收起审批详情；PageUp/PageDown 滚动
执行中可编辑草稿；回车不发送或排队。停止或结束后可提交。
"""
