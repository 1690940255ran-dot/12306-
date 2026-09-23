import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from railassist.adapters.browser.adapter import BrowserRailwayAdapter
from railassist.adapters.browser.session import BrowserSession
from railassist.adapters.browser.stations import StationCatalog
from railassist.adapters.mock import MockRailwayAdapter
from railassist.application.booking_service import BookingService
from railassist.application.sale_time_service import SaleTimeService
from railassist.application.scheduler import QueryScheduler
from railassist.application.task_service import TaskService
from railassist.application.waitlist_service import WaitlistService
from railassist.infrastructure.database import SQLiteTaskRepository
from railassist.infrastructure.instance_lock import InstanceLock
from railassist.infrastructure.logging_setup import setup_logging
from railassist.infrastructure.notifications import LogNotifier, OutboxStore


def default_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "RailAssist"


@dataclass
class Application:
    repository: SQLiteTaskRepository
    railway: object  # MockRailwayAdapter | BrowserRailwayAdapter
    tasks: TaskService
    sale_times: SaleTimeService
    booking: BookingService
    waitlist: WaitlistService
    outbox: OutboxStore
    scheduler: QueryScheduler
    logger: object
    adapter_kind: str

    def verified_capabilities(self) -> dict[str, bool]:
        return {
            row["name"]: row["verified"]
            for row in self.repository.list_capabilities()
            if row["adapter"] == self.adapter_kind
        }


def _build_railway(kind: str, data_dir: Path, repository: SQLiteTaskRepository,
                   remember: bool):
    if kind == "mock":
        return MockRailwayAdapter()
    session = BrowserSession(data_dir, remember=remember)
    session.start()
    catalog = StationCatalog(data_dir / "cache")
    verified = {
        row["name"]: row["verified"]
        for row in repository.list_capabilities() if row["adapter"] == "browser"
    }
    return BrowserRailwayAdapter(session, catalog, verified=verified)


@contextmanager
def create_application(data_dir: Path, adapter: str = "mock", remember: bool = False):
    if adapter not in ("mock", "browser"):
        raise ValueError(f"未知适配器：{adapter}")
    data_dir.mkdir(parents=True, exist_ok=True)
    with InstanceLock(data_dir / "instance.lock"):
        logger = setup_logging(data_dir / "logs")
        repository = None
        railway = None
        try:
            repository = SQLiteTaskRepository(data_dir / "app.db")
            outbox = OutboxStore(repository.connection)
            scheduler = QueryScheduler()
            notifier = LogNotifier(logger)
            logger.info("start", extra={"event": "application_started", "adapter": adapter})
            railway = _build_railway(adapter, data_dir, repository, remember)
            booking = BookingService(repository, railway, outbox)
            tasks = TaskService(repository, railway, notifier, outbox,
                                scheduler=scheduler, booking=booking)
            sale_times = SaleTimeService(repository, railway, outbox)
            waitlist = WaitlistService(booking)
            yield Application(repository, railway, tasks, sale_times, booking,
                              waitlist, outbox, scheduler, logger, adapter)
        finally:
            if railway is not None:
                railway.close()
            if repository is not None:
                repository.close()
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
