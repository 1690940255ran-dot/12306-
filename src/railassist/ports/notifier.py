from typing import Protocol


class NotifierPort(Protocol):
    def notify(self, event: str, task_id: str, message: str) -> None: ...

