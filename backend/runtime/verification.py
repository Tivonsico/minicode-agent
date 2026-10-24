"""Check fresh execution evidence; this is not an independent task verdict."""

MUTATIONS = frozenset({"WriteFile", "EditFile", "DeleteFile"})
VERSION = "fresh-verification-v2"
MAX_RECHECKS = 2
CONTRACT_REVIEW = (
    "执行验证已有新鲜通过记录，但这不能证明验收条件正确。结束前做一次契约复核："
    "逐项核对本轮新增的默认值、隐式假设、输入范围与原有异常路径。"
    "把需求要求改变的行为与仍应保留的行为分开，查阅已读接口约定和调用方；"
    "不能因为现有自测通过就认为新增假设成立。"
    "对推导、补全、查询等逻辑，信息不足时不能凭空制造结果；"
    "需求示例若缺少前提，说明这个缺口，不以猜测改变原接口。"
    "发现问题就用反例修复并重新验证，否则简洁交付。不扩展任务范围。"
)


def completion_check(events, turn_id):
    events = [event for event in events if event.get("turn_id") == turn_id]
    writes = [
        event
        for event in events
        if event["type"] == "tool_result"
        and event.get("name") in MUTATIONS
        and event.get("status") == "succeeded"
    ]
    checks = [event for event in events if event["type"] == "verification_result"]
    readbacks = [
        event
        for event in events
        if event["type"] == "file_verification_result" and event.get("status") == "passed"
    ]
    write = writes[-1] if writes else None
    check = checks[-1] if checks else None
    reason = "no_file_mutation"
    if write:
        if check is None:
            reason = "verification_missing"
        elif check["seq"] < write["seq"]:
            reason = "verification_stale"
        elif check.get("status") != "passed":
            reason = "verification_failed"
        else:
            reason = "fresh_verification"
        if reason in {"verification_missing", "verification_stale"} and all(
            (
                check is not None
                and check.get("status") == "passed"
                and check["seq"] > mutation["seq"]
            )
            or any(
                readback["seq"] > mutation["seq"]
                and {
                    "call_id": mutation.get("call_id"),
                    "request_id": mutation.get("request_id"),
                }
                in readback.get("mutations", [])
                for readback in readbacks
            )
            for mutation in writes
        ):
            reason = "combined_verification" if check is not None else "text_readback_verified"
            check = readbacks[-1]
    return {
        "version": VERSION,
        "scope": "file_tool_mutations",
        "required": bool(write),
        "accepted": reason
        in {
            "no_file_mutation",
            "fresh_verification",
            "text_readback_verified",
            "combined_verification",
        },
        "reason": reason,
        "last_mutation_seq": write["seq"] if write else None,
        "verification_seq": check["seq"] if check else None,
    }


def feedback(check):
    return (
        f"完成前核对未通过：{check['reason']}。"
        "请针对最后一次文件修改执行实际验证，Shell 设置 purpose=verification，"
        "用断言核对需求与原有异常行为，并检查真正的退出状态。"
        "若验证失败先修复；不要通过改变预期或打印成功来满足核对。"
        "无法验证时说明具体阻碍，不宣称测试通过。"
    )


class CompletionReview:
    """Track verification retries and the one contract review for a turn."""

    def __init__(self, store, turn_id):
        self.store, self.turn_id = store, turn_id
        self.feedback = ""
        self.rechecks = 0
        self.review_requested = False

    @property
    def exhausted(self):
        return self.rechecks > MAX_RECHECKS

    def evaluate(self, request_id, message_id):
        check = completion_check(self.store.view, self.turn_id)
        if not check["required"]:
            return False
        review = (
            check["reason"] in {"fresh_verification", "combined_verification"}
            and not self.review_requested
        )
        blocked = not check["accepted"] or review
        self.store.append(
            "completion_check",
            request_id=request_id,
            message_id=message_id,
            status="review_requested" if review else "blocked" if blocked else "accepted",
            review_requested=review,
            **check,
        )
        if review:
            self.review_requested = True
            self.feedback = CONTRACT_REVIEW
        else:
            self.feedback = feedback(check) if blocked else ""
        if not check["accepted"]:
            self.rechecks += 1
        return blocked
