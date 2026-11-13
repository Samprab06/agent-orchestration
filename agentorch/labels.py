"""GitHub labels used for coordination."""

BOARD = "board"
QUEUED = "agent:queued"
APPROVED = "agent:approved"
RUNNING = "agent:running"
REVIEW = "agent:review"
FAILED = "agent:failed"
CANCELLED = "agent:cancelled"
BLOCKED = "agent:blocked"

ALL = {
    BOARD: ("0e8a16", "The pinned agent message board"),
    QUEUED: ("1d76db", "Task for the server agent"),
    APPROVED: ("0e8a16", "An approver cleared this task to run"),
    RUNNING: ("fbca04", "The server agent is working on it"),
    REVIEW: ("5319e7", "Draft PR open for review"),
    FAILED: ("d93f0b", "The last attempt failed"),
    CANCELLED: ("cfd3d7", "Cancelled"),
    BLOCKED: ("b60205", "Waiting on a dependency"),
}

STATUS = (RUNNING, REVIEW, FAILED, CANCELLED)
