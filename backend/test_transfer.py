from types import SimpleNamespace

from app.services import transfer


async def test_transfer_failure_skips_file_and_continues(monkeypatch):
    task = SimpleNamespace(
        source_account_id=1,
        target_account_id=2,
        source_dir_path="/source",
        target_dir_path="/target",
        status="pending",
        last_run_at=None,
    )
    accounts = [
        SimpleNamespace(id=1, cookie="source-cookie"),
        SimpleNamespace(id=2, cookie="target-cookie"),
    ]
    committed_statuses = []
    execute_count = 0

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _task_id):
            return task

        async def execute(self, _query):
            nonlocal execute_count
            execute_count += 1
            if execute_count == 1:
                task.status = "running"
                task.last_run_at = "started"
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: accounts)
            )

        async def commit(self):
            committed_statuses.append(task.status)
            return None

    class Client:
        async def user_base_info(self, **_kwargs):
            return {"vip": True}

        async def fs_dir_getid_app(self, path, **_kwargs):
            return {"id": 1 if path == "/source" else 2}

    record_status = StatusRecorder()
    monkeypatch.setattr(transfer, "async_session", Session)
    monkeypatch.setattr(transfer, "create_p115_client", lambda _cookie: Client())
    monkeypatch.setattr(transfer, "_set_task_status", record_status)
    monkeypatch.setattr(transfer, "check_response", lambda response: response)

    file_infos = [
        {"id": "failed", "name": "failed.txt", "size": 1},
        {"id": "next", "name": "next.txt", "size": 1},
    ]
    wait_durations = []
    wait_ranges = []

    async def iter_files(*_args, **_kwargs):
        for file_info in file_infos:
            yield file_info

    monkeypatch.setattr(transfer.tool, "iter_files", iter_files)

    def random_delay(lower, upper):
        wait_ranges.append((lower, upper))
        return lower

    monkeypatch.setattr(transfer.random, "uniform", random_delay)

    async def record_sleep(duration):
        wait_durations.append(duration)

    monkeypatch.setattr(transfer.asyncio, "sleep", record_sleep)
    transferred_files = []

    async def transfer_file(_source, _target, file_info, **_kwargs):
        if file_info["id"] == "failed":
            raise RuntimeError("simulated transfer failure")
        transferred_files.append(file_info["id"])
        return {"state": True}

    monkeypatch.setattr(transfer.tool.edit, "transferfile", transfer_file)

    result = await transfer.run_transfer_task(123)

    assert result == 1
    assert transferred_files == ["next"]
    assert "running" in committed_statuses
    assert task.last_run_at is not None
    assert record_status.statuses == [(123, "failed")]
    assert wait_durations == [3, 3]
    assert wait_ranges == [(3, 10), (3, 10)]


async def test_running_transfer_task_is_skipped(monkeypatch):
    task = SimpleNamespace(status="running")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return SimpleNamespace(rowcount=0)

        async def get(self, _model, _task_id):
            return task

    monkeypatch.setattr(transfer, "async_session", Session)

    result = await transfer.run_transfer_task(123)

    assert result == 0
    assert task.status == "running"


class StatusRecorder:
    def __init__(self):
        self.statuses = []

    async def __call__(self, task_id, status):
        self.statuses.append((task_id, status))
