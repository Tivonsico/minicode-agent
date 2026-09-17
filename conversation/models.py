from enum import StrEnum


class TurnStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"


class MessageStatus(StrEnum):
    STREAMING = "streaming"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"
    CANCELLED = "cancelled"
