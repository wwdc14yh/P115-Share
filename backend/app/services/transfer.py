import asyncio
import random
from collections.abc import Mapping
from datetime import UTC, datetime
from numbers import Real

from app.core.database import async_session
from app.models.schema import P115Account, ScheduledTransferTask
from loguru import logger
from p115client import P115Client, check_response, tool
from sqlalchemy import select, update

NON_MEMBER_MAX_FILE_SIZE = 5 * 1024**3


def create_p115_client(cookies: str) -> P115Client:
    """使用账号表中的 Cookie 创建 p115client 客户端。"""
    return P115Client(cookies=cookies)


async def _set_task_status(task_id: int, status: str) -> None:
    """在独立会话中更新任务状态，避免长时间转存占用数据库连接。"""
    async with async_session() as session:
        task = await session.get(ScheduledTransferTask, task_id)
        if task is None:
            raise ValueError(f"Scheduled transfer task {task_id} no longer exists")
        task.status = status
        task.last_run_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()


def _directory_id(response: dict, path: str) -> int:
    """从 fs_dir_getid_app 的成功响应中读取目录 ID，根目录 ID 0 也是合法值。"""
    try:
        directory_id = int(response["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"115 未返回目录路径 {path!r} 的有效目录 ID") from exc

    if directory_id < 0:
        raise ValueError(f"115 返回了无效的目录 ID：{directory_id}")
    return directory_id


def _is_vip_account(response: Mapping) -> bool:
    """从 P115Client.user_base_info 响应读取会员标记。"""
    vip_flags: list[bool] = []
    pending: list[object] = [response]

    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "vip":
                    if item is None:
                        raise ValueError("115 用户信息中的 vip 字段为空，无法判断会员状态")
                    vip_flags.append(
                        str(item).strip().lower() not in {"", "0", "false", "no", "none", "null"}
                    )
                elif key == "level_name":
                    if item is None:
                        raise ValueError("115 用户信息中的会员等级为空，无法判断会员状态")
                    vip_flags.append(
                        str(item).strip().lower() not in {"", "0", "false", "no", "none", "null"}
                    )
                elif key in {"is_vip", "is_svip", "is_forever_vip", "is_forever"}:
                    if isinstance(item, bool):
                        vip_flags.append(item)
                    elif isinstance(item, Real):
                        vip_flags.append(item > 0)
                    elif isinstance(item, str) and item.strip().lower() in {
                        "1", "true", "yes"
                    }:
                        vip_flags.append(True)
                    elif isinstance(item, str) and item.strip().lower() in {
                        "0", "false", "no", ""
                    }:
                        vip_flags.append(False)
                    else:
                        raise ValueError(f"115 返回了无法识别的会员状态：{item!r}")
                elif isinstance(item, (Mapping, list, tuple)):
                    pending.append(item)
        elif isinstance(value, (list, tuple)):
            pending.extend(value)

    if not vip_flags:
        raise ValueError("115 用户信息响应中没有会员状态字段，无法安全判断文件大小限制")
    return any(vip_flags)


def _file_size_bytes(file_info: Mapping) -> int:
    """读取 p115client 文件属性中的字节数；缺失或非法数据不能默认为小文件。"""
    size = file_info.get("size")
    if isinstance(size, bool) or not isinstance(size, Real) or size < 0:
        raise ValueError(f"115 返回了无效的文件大小：{size!r}")
    return int(size)


def _format_file_size(size_bytes: int) -> str:
    """将字节数格式化为易读的 GiB 数值，通知中明确单位。"""
    return f"{size_bytes / 1024**3:.2f} GiB"


async def run_transfer_task(task_id: int) -> int:
    """递归转移任务源目录中的文件到目标目录，并返回成功转移的文件数。"""
    async with async_session() as session:
        started_at = datetime.now(UTC).replace(tzinfo=None)
        result = await session.execute(
            update(ScheduledTransferTask)
            .where(
                ScheduledTransferTask.id == task_id,
                ScheduledTransferTask.status != "running",
            )
            .values(status="running", last_run_at=started_at)
        )
        if result.rowcount == 0:
            task = await session.get(ScheduledTransferTask, task_id)
            if task is None:
                raise ValueError(f"Scheduled transfer task {task_id} not found")
            logger.info(f"跨账号转存任务 {task_id} 已在运行中，跳过本次执行")
            return 0

        await session.commit()
        task = await session.get(ScheduledTransferTask, task_id)
        if task is None:
            raise ValueError(f"Scheduled transfer task {task_id} not found")

        try:
            if task.source_account_id == task.target_account_id:
                raise ValueError("源账号和目标账号不能相同")

            account_ids = {task.source_account_id, task.target_account_id}
            result = await session.execute(
                select(P115Account).where(P115Account.id.in_(account_ids))
            )
            accounts = {account.id: account for account in result.scalars().all()}
            source_account = accounts.get(task.source_account_id)
            target_account = accounts.get(task.target_account_id)
            if source_account is None:
                raise ValueError(f"源账号 {task.source_account_id} 不存在")
            if target_account is None:
                raise ValueError(f"目标账号 {task.target_account_id} 不存在")
            if not source_account.cookie:
                raise ValueError(f"源账号 {task.source_account_id} 未配置 Cookie")
            if not target_account.cookie:
                raise ValueError(f"目标账号 {task.target_account_id} 未配置 Cookie")
            if not task.source_dir_path.strip():
                raise ValueError("源目录路径不能为空")
            if not task.target_dir_path.strip():
                raise ValueError("目标目录路径不能为空")

            # 只将后续网络操作所需的标量值带出会话，整个文件传输过程不持有 DB 连接。
            source_cookie = source_account.cookie
            target_cookie = target_account.cookie
            source_path = task.source_dir_path
            target_path = task.target_dir_path
        except Exception:
            task.status = "failed"
            task.last_run_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
            logger.exception(f"跨账号转存任务 {task_id} 初始化失败")
            raise

    try:
        source_client = create_p115_client(source_cookie)
        target_client = create_p115_client(target_cookie)

        # 转存的写入目标是目标账号，因此用目标账号权益决定单文件大小限制。
        # 用户基本信息接口返回成功但缺少会员字段时，直接报错，避免不确定时漏转大文件。
        user_info = check_response(
            await target_client.user_base_info(async_=True)
        )
        target_is_vip = _is_vip_account(user_info)
        if target_is_vip:
            logger.info(f"跨账号转存任务 {task_id}：目标账号为会员，不应用 5 GiB 单文件限制")
        else:
            logger.info(f"跨账号转存任务 {task_id}：目标账号为非会员，超过 5 GiB 的文件将跳过")

        # fs_dir_getid_app 只解析目录；check_response 会在路径不存在或不可访问时
        # 显式抛出 p115client 的响应错误，因此不能把失败误当成空目录处理。
        source_dir_response = check_response(
            await source_client.fs_dir_getid_app(source_path, async_=True)
        )
        target_dir_response = check_response(
            await target_client.fs_dir_getid_app(target_path, async_=True)
        )
        source_dir_id = _directory_id(source_dir_response, source_path)
        target_dir_id = _directory_id(target_dir_response, target_path)

        # iter_files(cur=0, type=99) 会递归遍历源目录树并只产出文件；
        # 单工作者加短冷却，避免枚举阶段并发打出大量目录读取请求。
        files = tool.iter_files(
            source_client,
            source_dir_id,
            type=99,
            cur=0,
            max_workers=0,
            cooldown=1,
            async_=True,
        )

        transferred_count = 0
        skipped_large_file_count = 0
        failed_file_count = 0
        async for file_info in files:
            file_name = str(file_info.get("name") or file_info.get("id") or "未知文件")
            file_size = _file_size_bytes(file_info)

            if not target_is_vip and file_size > NON_MEMBER_MAX_FILE_SIZE:
                size_text = _format_file_size(file_size)
                warning = (
                    f"⏭️ 跨账号转存任务 {task_id}：目标账号为非会员，跳过超出 5 GiB "
                    f"限制的文件「{file_name}」（{size_text}）"
                )
                logger.warning(warning)
                skipped_large_file_count += 1

                # 通知管理员具体文件及大小，便于确认哪些文件因权益限制未转存。
                from app.services.tg_bot import tg_service

                await tg_service.send_admin_msg(warning)
                continue

            delay = random.uniform(3, 10)
            logger.info(
                f"跨账号转存任务 {task_id}：等待 {delay:.1f} 秒后转移文件 {file_name}"
            )
            await asyncio.sleep(delay)

            # file_info 是 p115client 迭代器返回的源文件信息字典；transferfile
            # 依据其中的文件 ID、名称和哈希读取源文件，并将其上传到目标目录 ID。
            try:
                response = await tool.edit.transferfile(
                    source_client,
                    target_client,
                    file_info,
                    pid=target_dir_id,
                    app="web",
                    async_=True,
                )
                check_response(response)
            except Exception:
                failed_file_count += 1
                logger.exception(
                    f"跨账号转存任务 {task_id}：转移 {file_name} 失败，跳过当前文件"
                )
                continue

            transferred_count += 1
            logger.info(
                f"跨账号转存任务 {task_id}：已转移 {file_name}"
            )

        if transferred_count == 0:
            logger.info(f"跨账号转存任务 {task_id}：没有文件成功转移")

        await _set_task_status(
            task_id, "failed" if failed_file_count else "success"
        )
        logger.info(
            f"跨账号转存任务 {task_id} 完成，共转移 {transferred_count} 个文件，"
            f"因非会员限制跳过 {skipped_large_file_count} 个文件，"
            f"转存失败跳过 {failed_file_count} 个文件"
        )
        return transferred_count
    except Exception:
        logger.exception(f"跨账号转存任务 {task_id} 执行失败")
        await _set_task_status(task_id, "failed")
        raise
