import asyncio
import logging
import os
import random
import re
import time
from collections import Counter
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from p115client import check_response
from sqlalchemy import select
from app.core.config import settings
from app.core.database import async_session
from app.models.schema import ScheduledShareTask
from app.services.p115 import p115_service

logger = logging.getLogger(__name__)

SOURCE_SNAPSHOT_ATTEMPTS = 6
SOURCE_SNAPSHOT_INTERVAL = 2
TRANSFER_STABLE_SAMPLES = 3
TRANSFER_NO_PROGRESS_TIMEOUT = 15 * 60
TRANSFER_ABSOLUTE_TIMEOUT = 2 * 60 * 60
SCHEDULED_SHARE_CLEANUP_DELAY = 60


class MoveProgressFailed(RuntimeError):
    pass


def parse_size_to_bytes(val) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s_val = str(val).strip().upper()
    if not s_val:
        return 0
    import re
    match = re.match(r"^([0-9.]+)\s*([A-Z]*B?)$", s_val)
    if not match:
        try:
            return int(float(s_val))
        except:
            return 0
    number, unit = match.groups()
    number = float(number)
    units = {
        "": 1, "B": 1,
        "K": 1024, "KB": 1024,
        "M": 1024**2, "MB": 1024**2,
        "G": 1024**3, "GB": 1024**3,
        "T": 1024**4, "TB": 1024**4,
        "P": 1024**5, "PB": 1024**5
    }
    return int(number * units.get(unit, 1))


def _snapshot_signature(snapshot: dict) -> tuple:
    items = tuple(sorted(
        (item["id"], item["name"], bool(item["is_dir"]))
        for item in snapshot["items"]
    ))
    stats = snapshot["stats"]
    return (
        items,
        stats["size"],
        stats["file_count"],
        stats["folder_count"],
    )


async def _get_existing_dir_cid(svc, path: str) -> int:
    """只解析已有目录，避免路径拼写错误时自动创建空源目录。"""
    normalized = str(path).strip().replace("\\", "/") or "/"
    if normalized == "/":
        return 0
    resp = await svc._api_call_with_timeout(
        svc.client.fs_dir_getid_app,
        {"path": normalized},
        async_=True,
        timeout=30,
        max_retries=2,
        label="scheduled_share_resolve_dir",
        **svc._get_ios_ua_kwargs(),
    )
    if svc._is_margin_response(resp):
        raise RuntimeError(f"解析目录 {normalized} 时触发限速: {resp}")
    check_response(resp)
    data = resp.get("data")
    candidates = [data, resp] if isinstance(data, dict) else [resp]
    for candidate in candidates:
        cid = (
            candidate.get("cid")
            or candidate.get("category_id")
            or candidate.get("file_id")
            or candidate.get("id")
        )
        if cid:
            return int(cid)
    raise FileNotFoundError(f"定时分享源目录不存在或无法解析: {normalized}")


async def _read_dir_snapshot(svc, cid: int) -> dict:
    """严格读取目录顶层结构和服务端递归统计。"""
    items = await svc._get_dir_items(cid, strict=True)
    resp = await svc._fs_category_get_with_fallback(cid, timeout=30)
    if svc._is_margin_response(resp):
        raise RuntimeError(f"读取目录 {cid} 统计时触发限速: {resp}")
    check_response(resp)
    return {
        "cid": cid,
        "items": items,
        "stats": {
            "size": parse_size_to_bytes(resp.get("size", 0)),
            "file_count": int(resp.get("count", 0) or resp.get("file_count", 0) or 0),
            "folder_count": int(resp.get("folder_count", 0) or 0),
        },
    }


async def _capture_stable_dir_snapshot(
    svc,
    cid: int,
    *,
    attempts: int = SOURCE_SNAPSHOT_ATTEMPTS,
    interval: float = SOURCE_SNAPSHOT_INTERVAL,
) -> dict:
    """连续两次读取一致后，才把目录状态作为任务基准。"""
    previous = None
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            current = await _read_dir_snapshot(svc, cid)
            if previous and _snapshot_signature(current) == _snapshot_signature(previous):
                return current
            previous = current
            last_error = None
        except Exception as exc:
            previous = None
            last_error = exc
            logger.warning("定时分享目录快照读取失败（第 %s/%s 次）: %s", attempt, attempts, exc)
        if attempt < attempts:
            await asyncio.sleep(interval)
    if last_error:
        raise RuntimeError(f"无法可靠读取目录 {cid} 快照: {last_error}") from last_error
    raise RuntimeError(f"目录 {cid} 在快照窗口内持续变化，延后本次定时分享")


def _extract_move_progress_id(move_resp: dict) -> str | None:
    if not isinstance(move_resp, dict):
        return None
    data = move_resp.get("data")
    candidates = [move_resp]
    if isinstance(data, dict):
        candidates.insert(0, data)
    for candidate in candidates:
        value = (
            candidate.get("move_proid")
            or candidate.get("move_pro_id")
            or candidate.get("pro_id")
            or candidate.get("task_id")
        )
        if value:
            return str(value)
    return None


async def _read_move_progress(svc, progress_id: str | None) -> tuple[bool, tuple | None]:
    """返回是否允许完成，以及可用于判断进展的状态签名。"""
    if not progress_id or not hasattr(svc.client, "fs_move_progress"):
        return True, None
    resp = await svc._api_call_with_timeout(
        svc.client.fs_move_progress,
        progress_id,
        async_=True,
        timeout=30,
        max_retries=1,
        label="scheduled_share_move_progress",
    )
    if svc._is_margin_response(resp):
        return False, None
    check_response(resp)
    data = resp.get("data", resp)
    if not isinstance(data, dict):
        return True, None
    status = str(
        data.get("status")
        or data.get("state")
        or data.get("move_status")
        or ""
    ).lower()
    if status in {"fail", "failed", "error", "-1"}:
        raise MoveProgressFailed(f"115 移动任务失败: {resp}")
    percent = data.get("percent", data.get("progress"))
    progress_signature = (status, str(percent))
    if percent is not None:
        try:
            if float(str(percent).rstrip("%")) < 100:
                return False, progress_signature
        except (TypeError, ValueError):
            pass
    return (
        status not in {"pending", "waiting", "running", "processing", "0", "1"},
        progress_signature,
    )


def _transfer_poll_interval(elapsed: float) -> float:
    if elapsed < 30:
        return 2
    if elapsed < 5 * 60:
        return 5
    return 20


async def _wait_transfer_complete(
    svc,
    *,
    mode: str,
    source_cid: int,
    target_cid: int,
    baseline: dict,
    move_progress_id: str | None = None,
    stable_required: int = TRANSFER_STABLE_SAMPLES,
    no_progress_timeout: float = TRANSFER_NO_PROGRESS_TIMEOUT,
    absolute_timeout: float = TRANSFER_ABSOLUTE_TIMEOUT,
    poll_interval_range: tuple[float, float] | None = None,
) -> dict:
    """等待移动/复制达到源快照，返回完成后的目标快照。"""
    started_at = time.monotonic()
    last_progress_at = started_at
    last_target_signature = None
    stable_times = 0
    structure_stable_times = 0
    last_progress_signature = None
    last_source_pending = None
    expected_ids = {item["id"] for item in baseline["items"]}
    expected_names = Counter(
        (item["name"], bool(item["is_dir"])) for item in baseline["items"]
    )
    expected_stats = baseline["stats"]

    while True:
        now = time.monotonic()
        elapsed = now - started_at
        if elapsed >= absolute_timeout:
            raise TimeoutError(f"{mode}模式等待传输完成超过 {absolute_timeout:.0f} 秒")
        if now - last_progress_at >= no_progress_timeout:
            raise TimeoutError(f"{mode}模式连续 {no_progress_timeout:.0f} 秒未检测到进展")

        try:
            target = await _read_dir_snapshot(svc, target_cid)
            target_signature = _snapshot_signature(target)
            if target_signature != last_target_signature:
                last_target_signature = target_signature
                last_progress_at = time.monotonic()

            target_ids = {item["id"] for item in target["items"]}
            target_names = Counter(
                (item["name"], bool(item["is_dir"])) for item in target["items"]
            )
            stats_match = target["stats"] == expected_stats
            progress_ready, progress_signature = await _read_move_progress(
                svc, move_progress_id if mode == "move" else None
            )
            if progress_signature and progress_signature != last_progress_signature:
                last_progress_signature = progress_signature
                last_progress_at = time.monotonic()

            if mode == "move":
                source_items = await svc._get_dir_items(source_cid, strict=True)
                source_ids = {item["id"] for item in source_items}
                source_pending = frozenset(source_ids & expected_ids)
                if source_pending != last_source_pending:
                    last_source_pending = source_pending
                    last_progress_at = time.monotonic()
                structure_match = (
                    target_ids == expected_ids
                    and not source_pending
                )
            else:
                structure_match = target_names == expected_names

            if structure_match and progress_ready:
                last_progress_at = time.monotonic()

            if stats_match and structure_match and progress_ready:
                stable_times += 1
                structure_stable_times = 0
                logger.info(
                    "定时分享 %s 模式完整性校验通过（连续 %s/%s 次）",
                    mode,
                    stable_times,
                    stable_required,
                )
                if stable_times >= stable_required:
                    return target
            elif mode == "move" and structure_match and progress_ready:
                stable_times = 0
                structure_stable_times += 1
                logger.info(
                    "定时分享 move 模式顶层结构与任务进度校验通过（连续 %s/%s 次，等待服务端统计同步中）",
                    structure_stable_times,
                    stable_required,
                )
                if structure_stable_times >= stable_required:
                    logger.warning(
                        "⚠️ 定时分享 move 模式顶层结构已完全就位 (%s 项)，但 115 服务端深层统计尚未同步完成 "
                        "(原基准: %s, 目标目录当前统计: %s)，降级放行",
                        len(expected_ids),
                        expected_stats,
                        target["stats"],
                    )
                    return target
            else:
                stable_times = 0
                structure_stable_times = 0
                logger.info(
                    "等待定时分享传输完成: mode=%s, stats=%s, structure=%s, progress=%s",
                    mode,
                    stats_match,
                    structure_match,
                    progress_ready,
                )
        except MoveProgressFailed:
            raise
        except Exception as exc:
            stable_times = 0
            structure_stable_times = 0
            logger.warning("定时分享传输完成检测失败，将继续轮询: %s", exc)

        if poll_interval_range:
            await asyncio.sleep(random.uniform(*poll_interval_range))
        else:
            await asyncio.sleep(_transfer_poll_interval(time.monotonic() - started_at))


async def _cleanup_scheduled_temp_dir(
    svc,
    *,
    cid: int,
    folder_name: str,
    enabled: bool,
) -> bool:
    """按任务开关删除临时目录，并清空账号回收站。"""
    if not enabled:
        logger.info("📁 未开启清理临时目录，本次保留目录 %s (CID: %s)", folder_name, cid)
        return False

    logger.info("⏳ 分享已创建，等待 %s 秒后删除临时目录...", SCHEDULED_SHARE_CLEANUP_DELAY)
    await asyncio.sleep(SCHEDULED_SHARE_CLEANUP_DELAY)
    logger.info("🗑️ 正在删除临时目录 %s (CID: %s)...", folder_name, cid)
    delete_resp = await svc._api_call_with_timeout(
        svc.client.fs_delete_app,
        [cid],
        async_=True,
        **svc._get_ios_ua_kwargs(),
    )
    check_response(delete_resp)
    logger.info("⏳ 等待 60 秒，确保临时目录进入回收站...")
    await asyncio.sleep(60)
    if not await svc.cleanup_recycle_bin():
        raise RuntimeError("临时目录已删除，但清空回收站失败")
    return True


TMDB_ID_PATTERN = re.compile(r"(?i)tmdb(?:id)?\s*[-_:= ]\s*(\d+)")
YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _extract_sunflower_tmdb_id(name: str) -> str | None:
    match = TMDB_ID_PATTERN.search(name)
    return match.group(1) if match else None


def _to_sunflower_pinyin(text: str) -> str:
    """将中文段转为小写全拼，其他字符原样保留。"""
    import pypinyin

    parts = re.findall(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+", text)
    return "".join(
        "".join(pypinyin.lazy_pinyin(part, style=pypinyin.Style.NORMAL))
        if re.fullmatch(r"[\u4e00-\u9fff]+", part)
        else part
        for part in parts
    )


def _sunflower_name_parts(name: str) -> tuple[str, str | None, str | None]:
    """返回标准化目录名、TMDB ID 和目录标题；无 TMDB ID 时保留原名。"""
    tmdb_id = _extract_sunflower_tmdb_id(name)
    if not tmdb_id:
        return name, None, None

    year_match = YEAR_PATTERN.search(name)
    year = year_match.group(1) if year_match else None
    title = TMDB_ID_PATTERN.sub("", name)
    title = YEAR_PATTERN.sub("", title)
    title = re.sub(r"[\[\]{}()（）【】]", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-")
    pinyin_title = _to_sunflower_pinyin(title).strip()
    standardized = pinyin_title
    if year:
        standardized += f" ({year})"
    if standardized:
        standardized += " "
    standardized += f"[tmdbid={tmdb_id}]"
    return standardized, tmdb_id, title


def _sunflower_file_name(
    file_name: str,
    *,
    source_dir_name: str,
    source_title: str,
    standardized_dir_name: str,
) -> str:
    """以规范目录名为文件前缀，并保留原文件的画质、集数和扩展名。"""
    stem, extension = os.path.splitext(file_name)
    if stem.casefold().startswith(standardized_dir_name.casefold()):
        return file_name

    remainder = stem
    # 115 中常见的剧集文件以原目录标题开头；去掉该标题后再添加标准化前缀，
    # 避免产生“拼音目录.中文原名.年份...”这样的双重标题。
    for title in sorted({source_dir_name, source_title}, key=len, reverse=True):
        if title and remainder.casefold().startswith(title.casefold()):
            remainder = remainder[len(title):]
            break
    remainder = TMDB_ID_PATTERN.sub("", remainder, count=1)
    remainder = remainder.lstrip(" ._-–—()[]{}（）【】")
    remainder = _to_sunflower_pinyin(remainder)
    if remainder:
        return f"{standardized_dir_name}.{remainder}{extension}"
    return f"{standardized_dir_name}{extension}"


async def _sunflower_pause(operation: str, minimum: float = 5, maximum: float = 10) -> None:
    delay = random.uniform(minimum, maximum)
    logger.info("⏱️ 葵花宝典模式：%s 前等待 %.1f 秒", operation, delay)
    await asyncio.sleep(delay)


async def _scan_sunflower_tree(svc, cid: int, initial_items: list | None = None) -> list[dict]:
    """递归读取目录树，返回所有文件的 ID、名称和父目录 ID。"""
    files = []

    async def walk(current_cid: int, items: list | None = None) -> None:
        if items is None:
            await _sunflower_pause(f"读取目录 {current_cid}")
            items = await svc._get_dir_items(current_cid, strict=True)
        for item in items:
            if item["is_dir"]:
                await walk(int(item["id"]))
            else:
                files.append({**item, "parent_cid": current_cid})

    await walk(cid, initial_items)
    return files


async def _run_sunflower_share_mode(svc, task_db, task_id: int, src_cid: int, items: list) -> None:
    """按直接子目录复制、脱敏、分享并清理；失败时保留对应原目录。"""
    from app.services.tg_bot import tg_service

    target_channels = task_db.target_channels or []
    if not target_channels:
        raise ValueError("葵花宝典模式必须配置至少一个推送频道，避免生成链接后无处推送")
    await tg_service.broadcast_to_channels(
        {},
        {},
        channel_ids=target_channels,
        raise_on_error=True,
        validate_only=True,
    )

    child_dirs = [item for item in items if item["is_dir"]]
    logger.info(
        "🌻 葵花宝典模式启动：源目录 CID=%s，发现 %s 个直接子目录，目标频道=%s",
        src_cid,
        len(child_dirs),
        target_channels,
    )
    if not child_dirs:
        logger.info("🌻 源目录下没有直接子目录，本次不创建分享链接")
        return

    # 先遍历所有候选子目录，确认其中至少包含一个文件；空目录不复制、不分享。
    eligible_dirs = []
    for child in child_dirs:
        child_cid = int(child["id"])
        child_files = await _scan_sunflower_tree(svc, child_cid)
        if not child_files:
            logger.info("📂 跳过空目录：%s (CID: %s)", child["name"], child_cid)
            continue
        logger.info(
            "📂 目录预检完成：%s (CID: %s)，递归文件数=%s",
            child["name"],
            child_cid,
            len(child_files),
        )
        eligible_dirs.append(child)

    if not eligible_dirs:
        logger.info("🌻 所有直接子目录均为空，本次任务正常结束")
        return

    failures = []
    for index, child in enumerate(eligible_dirs, start=1):
        child_cid = int(child["id"])
        source_name = child["name"]
        stage_cid = None
        share_created = False
        delivery_registered = False
        standardized_name, tmdb_id, source_title = _sunflower_name_parts(source_name)
        logger.info(
            "🌻 开始处理第 %s/%s 个目录：%s (CID: %s)，TMDB ID=%s",
            index,
            len(eligible_dirs),
            source_name,
            child_cid,
            tmdb_id or "未找到（跳过脱敏）",
        )

        try:
            # 再次读取即将复制的目录，避免预检后内容变化时复制到不完整快照。
            await _sunflower_pause(f"读取源目录快照 {source_name}")
            source_snapshot = await _read_dir_snapshot(svc, child_cid)
            source_files = await _scan_sunflower_tree(
                svc, child_cid, initial_items=source_snapshot["items"]
            )
            if not source_files:
                logger.info("📂 目录在预检后已变为空，跳过：%s", source_name)
                continue

            unique_suffix = f"{task_id}_{time.time_ns() % 1_000_000:06d}"
            staging_name = f"{source_name}（副本）_{unique_suffix}"
            await _sunflower_pause(f"创建副本目录 {staging_name}")
            create_resp = await svc._api_call_with_timeout(
                svc.client.fs_makedirs_app,
                staging_name,
                pid=src_cid,
                async_=True,
                **svc._get_ios_ua_kwargs(),
            )
            check_response(create_resp)
            create_data = create_resp.get("data")
            if not isinstance(create_data, dict):
                create_data = {}
            stage_cid = int(
                create_data.get("cid")
                or create_data.get("id")
                or create_resp.get("cid")
                or create_resp.get("id")
                or 0
            )
            if not stage_cid:
                raise RuntimeError(f"创建副本目录后未返回 CID: {create_resp}")
            logger.info("📁 副本目录已创建：%s (CID: %s)", staging_name, stage_cid)

            # 将原子目录的所有直接子项复制到副本根目录，保留其内部层级。
            source_ids = [item["id"] for item in source_snapshot["items"]]
            if not source_ids:
                raise RuntimeError(f"目录快照为空，拒绝创建空副本：{source_name}")
            await _sunflower_pause(f"复制目录 {source_name}")
            copy_resp = await svc._api_call_with_timeout(
                svc.client.fs_copy_app,
                source_ids,
                pid=stage_cid,
                async_=True,
                **svc._get_ios_ua_kwargs(),
            )
            check_response(copy_resp)
            await _wait_transfer_complete(
                svc,
                mode="copy",
                source_cid=child_cid,
                target_cid=stage_cid,
                baseline=source_snapshot,
                poll_interval_range=(5, 10),
            )
            logger.info("✅ 目录复制完成：%s -> %s (CID: %s)", source_name, staging_name, stage_cid)

            # 只有发现 TMDB 标识的目录才更改目录名和文件名；其他目录保持原名。
            if tmdb_id:
                await _sunflower_pause(f"重命名副本目录为 {standardized_name}")
                rename_resp = await svc._api_call_with_timeout(
                    svc.client.fs_rename_app,
                    (stage_cid, standardized_name),
                    async_=True,
                    **svc._get_ios_ua_kwargs(),
                )
                check_response(rename_resp)
                logger.info("✏️ 副本目录脱敏完成：%s -> %s", source_name, standardized_name)
            else:
                await _sunflower_pause(f"移除副本目录后缀并恢复目录名 {source_name}")
                rename_resp = await svc._api_call_with_timeout(
                    svc.client.fs_rename_app,
                    (stage_cid, source_name),
                    async_=True,
                    **svc._get_ios_ua_kwargs(),
                )
                check_response(rename_resp)
                logger.info("ℹ️ 目录未包含 TMDB ID，已移除副本后缀，保留原名：%s", source_name)

            if tmdb_id:
                copied_snapshot = await _read_dir_snapshot(svc, stage_cid)
                copied_files = await _scan_sunflower_tree(
                    svc, stage_cid, initial_items=copied_snapshot["items"]
                )
                rename_items = []
                for copied_file in copied_files:
                    new_name = _sunflower_file_name(
                        copied_file["name"],
                        source_dir_name=source_name,
                        source_title=source_title or "",
                        standardized_dir_name=standardized_name,
                    )
                    if new_name != copied_file["name"]:
                        rename_items.append((copied_file["id"], new_name, copied_file["name"]))

                logger.info(
                    "📝 递归文件脱敏完成：待重命名 %s 个文件（总文件 %s 个）",
                    len(rename_items),
                    len(copied_files),
                )
                for batch_start in range(0, len(rename_items), 100):
                    batch = rename_items[batch_start:batch_start + 100]
                    await _sunflower_pause(
                        f"批量重命名文件 {batch_start + 1}-{batch_start + len(batch)}"
                    )
                    rename_files_resp = await svc._api_call_with_timeout(
                        svc.client.fs_rename_app,
                        [(fid, new_name) for fid, new_name, _ in batch],
                        async_=True,
                        **svc._get_ios_ua_kwargs(),
                    )
                    check_response(rename_files_resp)
                    for _, new_name, old_name in batch:
                        logger.info("✏️ 文件名脱敏：%s -> %s", old_name, new_name)

            await _sunflower_pause(f"为副本目录 {standardized_name} 创建分享链接")
            share_link = await svc._share_fids_direct([stage_cid])
            if not share_link:
                raise RuntimeError(f"为副本目录 {standardized_name} 创建分享链接失败")
            share_created = True
            logger.info("🔗 分享链接创建成功：目录=%s，链接=%s", standardized_name, share_link)

            full_text = f"🌻 葵花宝典定时分享：{standardized_name}\n🔗 链接：{share_link}"
            metadata = {"full_text": full_text, "entities": [], "photo_id": None}
            await _sunflower_pause(f"推送分享链接到频道 {target_channels}")
            await tg_service.broadcast_to_channels(
                {share_link: share_link},
                metadata,
                channel_ids=target_channels,
                raise_on_error=True,
            )
            delivery_registered = True
            logger.info("📢 分享链接已直接推送到频道（不检查审核状态）：%s", share_link)

            await _sunflower_pause(
                f"频道推送完成后的后续操作 {share_link}",
                minimum=180,
                maximum=420,
            )

            # 该模式明确要求分享创建后立即删除副本和原子目录；不清空整个回收站，
            # 以免误删该账号中与本任务无关的已删除文件。
            if not delivery_registered:
                raise RuntimeError("频道推送未登记，保留原目录和副本")
            await _sunflower_pause(f"删除副本和原目录 {source_name}")
            delete_resp = await svc._api_call_with_timeout(
                svc.client.fs_delete_app,
                [stage_cid, child_cid],
                async_=True,
                **svc._get_ios_ua_kwargs(),
            )
            check_response(delete_resp)
            logger.info(
                "🗑️ 副本与原子目录已移入回收站：副本 CID=%s，原目录 CID=%s",
                stage_cid,
                child_cid,
            )

        except Exception as exc:
            logger.error(  # noqa: G201
                "❌ 葵花宝典模式处理目录失败：%s (CID: %s)，分享创建=%s，推送登记=%s，错误=%s",
                source_name,
                child_cid,
                share_created,
                delivery_registered,
                exc,
                exc_info=True,
            )
            # 只有分享未创建时才清理临时副本；分享创建或推送失败时保留文件以便恢复。
            if stage_cid and not share_created:
                try:
                    await _sunflower_pause(f"失败后清理未分享副本 CID={stage_cid}")
                    cleanup_resp = await svc._api_call_with_timeout(
                        svc.client.fs_delete_app,
                        [stage_cid],
                        async_=True,
                        **svc._get_ios_ua_kwargs(),
                    )
                    check_response(cleanup_resp)
                    logger.info("🗑️ 未分享的失败副本已移入回收站：CID=%s", stage_cid)
                except Exception as cleanup_exc:
                    logger.error(
                        "❌ 清理未分享副本失败：CID=%s，错误=%s",
                        stage_cid,
                        cleanup_exc,
                        exc_info=True,
                    )
            failures.append(f"{source_name}: {exc}")
            continue

        if index < len(eligible_dirs):
            delay = random.uniform(180, 420)
            logger.info(
                "⏳ 当前目录分享与清理完成，等待 %.1f 秒（3-7 分钟）后处理下一个目录",
                delay,
            )
            await asyncio.sleep(delay)

    if failures:
        raise RuntimeError(
            f"葵花宝典模式有 {len(failures)} 个目录处理失败：{'；'.join(failures)}"
        )
    logger.info("🌻 葵花宝典模式处理完成：成功分享 %s 个目录", len(eligible_dirs))


def _get_all_svcs():
    """获取所有启用账号的 P115Service 列表，降级到全局单例"""
    try:
        from app.services.account_manager import account_manager
        svcs = account_manager.get_all_services()
        if svcs:
            return svcs
    except Exception:
        pass
    return [p115_service]


async def _scheduled_cleanup_save_dir(wait: bool = False):
    """定时清理保存目录——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.cleanup_save_directory(wait)
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 清理保存目录失败: {e}")


async def _scheduled_cleanup_recycle_bin(wait: bool = False):
    """定时清空回收站——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.cleanup_recycle_bin(wait)
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 清空回收站失败: {e}")


async def _scheduled_capacity_check():
    """定时容量检测——遍历所有账号分别执行"""
    for svc in _get_all_svcs():
        try:
            await svc.check_capacity_and_cleanup(mode="scheduled")
        except Exception as e:
            acc_id = svc.account.id if getattr(svc, 'account', None) else '?'
            logger.error(f"[ERROR] 账号 [{acc_id}] 容量检测失败: {e}")


async def _scheduled_session_expiry_check():
    """定时检测所有 115 账号的登录 Session 有效性并进行 TG 告警通知"""
    if not settings.TG_BOT_TOKEN:
        logger.debug("[SessionCheck] TG Bot Token 未配置，跳过 Session 失效检测")
        return

    failed_accounts = []
    for svc in _get_all_svcs():
        try:
            # 这里的 verify_connection 会发送请求验证 Cookie 是否有效
            is_ok = await svc.verify_connection()
            if not is_ok:
                acc_name = svc.account.name if (svc.account and svc.account.name) else f"账号(ID: {svc.account.id})" if svc.account else "默认账号"
                failed_accounts.append(acc_name)
        except Exception as e:
            acc_name = svc.account.name if (svc.account and svc.account.name) else f"账号(ID: {svc.account.id})" if svc.account else "默认账号"
            logger.error(f"[SessionCheck] 检测账号 [{acc_name}] 登录状态失败: {e}")
            failed_accounts.append(acc_name)

    if failed_accounts:
        try:
            from app.services.tg_bot import tg_service
            logger.warning(f"[SessionCheck] 检测到有账号登录失效: {failed_accounts}，准备发送通知")
            accounts_str = "\n".join([f"- {name}" for name in failed_accounts])
            msg = (
                f"⚠️ **115 账号登录状态失效提醒**\n\n"
                f"以下账号的登录状态已失效，请及时在 [账号管理] 或设置中更新 Cookie：\n"
                f"{accounts_str}"
            )
            await tg_service.send_admin_msg(msg)
        except Exception as e:
            logger.error(f"[SessionCheck] 发送 TG 账号失效通知失败: {e}")


async def _check_scheduled_share_status(svc, share_link: str) -> bool:
    """校验新生成的分享链接，返回其是否仍需等待审核或快照生成。"""
    status = await svc.get_share_status(share_link)
    if status is None:
        raise RuntimeError(f"无法检查新分享链接状态: {share_link}")
    if status.get("is_expired"):
        raise RuntimeError(f"新生成的分享链接已过期: {share_link}")
    if status.get("is_prohibited"):
        raise RuntimeError(f"新生成的分享链接包含违规内容: {share_link}")
    return bool(status.get("is_pending"))


async def _run_scheduled_share_task(task_id: int):
    """定时移动/复制目录并分享，验证成功后将临时目录移入回收站。"""
    # 1. 查询任务信息
    async with async_session() as session:
        result = await session.execute(select(ScheduledShareTask).where(ScheduledShareTask.id == task_id))
        task = result.scalar_one_or_none()
        if not task or not task.enabled:
            return
        
    # 2. 获取网盘账号 service
    from app.services.account_manager import account_manager
    svc = await account_manager.get_service_by_id(task.account_id)
    if not svc:
        raise ValueError(f"网盘账号 (ID: {task.account_id}) 不存在")

    async def _execute_flow():
        # 再次获取任务对象以更新其状态为运行中
        async with async_session() as session:
            task_db = await session.get(ScheduledShareTask, task_id)
            if not task_db or not task_db.enabled:
                return
            task_db.status = "running"
            await session.commit()

        logger.info(f"🚀 开始执行定时分享任务 [{task_id}] '{task_db.name}'")
        
        try:
            # 3. 解析源目录及其父路径
            src_path = task_db.dir_path.strip().rstrip('/')
            if not src_path.startswith('/'):
                src_path = '/' + src_path
            if src_path == '/':
                raise ValueError("不能对根目录执行定时分享")
                
            parent_path, folder_name = os.path.split(src_path)
            if not parent_path:
                parent_path = '/'
                
            logger.info(f"📂 源目录: {src_path}, 父路径: {parent_path}, 文件夹名: {folder_name}")
            
            # 4. 只解析已有目录，避免配置错误时自动创建空源目录
            share_mode = task_db.share_mode if (hasattr(task_db, "share_mode") and task_db.share_mode) else ("move" if task_db.clear_files else "copy")
            if share_mode == "sunflower":
                await _sunflower_pause(f"验证源目录的父目录 {parent_path}")
            parent_cid = await _get_existing_dir_cid(svc, parent_path)
            if share_mode == "sunflower":
                await _sunflower_pause(f"验证源目录 {src_path}")
            src_cid = await _get_existing_dir_cid(svc, src_path)

            # 5. 移动/复制模式必须先建立连续稳定的源目录基准
            baseline = None
            if share_mode in {"move", "copy"}:
                baseline = await _capture_stable_dir_snapshot(svc, src_cid)
                items = baseline["items"]
            else:
                if share_mode == "sunflower":
                    await _sunflower_pause(f"读取待处理源目录 {src_path}")
                items = await svc._get_dir_items(src_cid, strict=True)
            top_ids = {item["id"] for item in items}
            if not top_ids:
                logger.warning(f"⚠️ 源目录 {src_path} 为空，跳过定时分享。")
                async with async_session() as session:
                    task_db_empty = await session.get(ScheduledShareTask, task_id)
                    if task_db_empty:
                        task_db_empty.status = "success"
                        task_db_empty.last_run_at = datetime.utcnow()
                        await session.commit()
                return
                
            # 5.5 如果设置了容量限制，进行源目录容量检测
            try:
                min_size_val = float(getattr(task_db, "min_size", 0.0) or 0.0)
            except (ValueError, TypeError):
                min_size_val = 0.0

            if min_size_val > 0.0:
                logger.info(f"⚖️ 开始检测源目录容量 (阈值: {min_size_val} {task_db.min_size_unit})...")
                try:
                    if baseline:
                        dir_size = baseline["stats"]["size"]
                    else:
                        if share_mode == "sunflower":
                            await _sunflower_pause(f"检查源目录容量 {src_path}")
                        cat_resp = await svc._api_call_with_timeout(
                            svc.client.fs_category_get_app, src_cid, async_=True,
                            **svc._get_ios_ua_kwargs()
                        )
                        check_response(cat_resp)
                        dir_size = parse_size_to_bytes(cat_resp.get("size", 0))
                    
                    # 将任务设定的阈值换算为字节
                    unit = getattr(task_db, "min_size_unit", "GB").upper()
                    threshold_bytes = min_size_val * (1024**4 if unit == "TB" else 1024**3)
                    
                    logger.info(f"📊 源目录实际大小: {dir_size / (1024**3):.3f} GB ({dir_size} 字节), 设定阈值: {min_size_val} {unit} ({threshold_bytes} 字节)")
                    
                    if dir_size < threshold_bytes:
                        logger.warning(f"⚠️ 源目录实际容量未达到阈值，跳过本次分享。")
                        async with async_session() as session:
                            task_db_cap = await session.get(ScheduledShareTask, task_id)
                            if task_db_cap:
                                task_db_cap.status = "success"
                                task_db_cap.last_run_at = datetime.utcnow()
                                await session.commit()
                        return
                except Exception as size_e:
                    logger.error(f"❌ 获取源目录容量失败: {size_e}，将直接进行备份分享。")

            if share_mode == "sunflower":
                await _run_sunflower_share_mode(
                    svc,
                    task_db,
                    task_id,
                    src_cid,
                    items,
                )
                async with async_session() as session:
                    task_db_succ = await session.get(ScheduledShareTask, task_id)
                    if task_db_succ:
                        task_db_succ.status = "success"
                        task_db_succ.last_run_at = datetime.utcnow()
                        await session.commit()
                logger.info(
                    "🎉 葵花宝典定时分享任务 [%s] '%s' 执行完毕！",
                    task_id,
                    task_db.name,
                )
                return

            if share_mode == "direct":
                logger.info(f"🔗 [直接分享模式] 正在为 {folder_name} (CID: {src_cid}) 创建分享链接...")
                await asyncio.sleep(5)
                share_link = await svc._share_fids_direct([src_cid])
                if not share_link:
                    raise RuntimeError("创建分享链接失败")
                logger.info(f"✅ 生成定时分享链接成功: {share_link}")
            else:
                # 6. 新建带时间戳的目标目录
                timestamp = time.strftime('%Y%m%d_%H%M%S')
                unique_suffix = f"{task_id}_{time.time_ns() % 1_000_000:06d}"
                new_folder_name = f"{folder_name}_{timestamp}_{unique_suffix}"
                
                logger.info(f"📁 新建复制目标文件夹: {new_folder_name}")
                sub_dir_resp = await svc._api_call_with_timeout(
                    svc.client.fs_makedirs_app, new_folder_name, pid=parent_cid, async_=True,
                    **svc._get_ios_ua_kwargs()
                )
                check_response(sub_dir_resp)
                new_cid = int(sub_dir_resp.get("cid") or sub_dir_resp.get("id") or (sub_dir_resp.get("data") or {}).get("cid") or 0)
                if not new_cid:
                    raise RuntimeError("创建目标复制子目录失败")
                empty_target = await _capture_stable_dir_snapshot(svc, new_cid)
                if empty_target["items"] or any(empty_target["stats"].values()):
                    raise RuntimeError(f"新建目标目录 {new_cid} 并非空目录，拒绝继续")
                    
                # 7. 根据模式移动或复制顶级文件/目录到新目录
                fids = list(top_ids)
                move_progress_id = None
                if share_mode == "move":
                    logger.info(f"📤 [移动模式] 正在移动顶级项 {len(fids)} 个到 {new_folder_name} (CID: {new_cid})...")
                    move_resp = await svc._api_call_with_timeout(
                        svc.client.fs_move_app, fids, pid=new_cid, async_=True,
                        **svc._get_ios_ua_kwargs()
                    )
                    check_response(move_resp)
                    move_progress_id = _extract_move_progress_id(move_resp)
                else:
                    logger.info(f"📤 [复制模式] 正在复制顶级项 {len(fids)} 个到 {new_folder_name} (CID: {new_cid})...")
                    copy_resp = await svc._api_call_with_timeout(
                        svc.client.fs_copy_app, fids, pid=new_cid, async_=True,
                        **svc._get_ios_ua_kwargs()
                    )
                    check_response(copy_resp)

                # 8. 等待目标目录与传输前快照连续一致
                await _wait_transfer_complete(
                    svc,
                    mode=share_mode,
                    source_cid=src_cid,
                    target_cid=new_cid,
                    baseline=baseline,
                    move_progress_id=move_progress_id,
                )
                
                # 8.5 敏感词替换（若启用）
                if task_db.sensitive_replace_enabled:
                    await svc.replace_sensitive_words_in_dir(
                        new_cid,
                        replace_enabled=task_db.sensitive_replace_enabled,
                        replace_pinyin=task_db.sensitive_replace_pinyin,
                        replace_tmdb=task_db.sensitive_replace_tmdb
                    )

                # 改名也可能异步执行，尝试重新取得稳定的最终目标快照（允许降级跳过以保障分享、推送和后续步骤）
                try:
                    final_target_snapshot = await _capture_stable_dir_snapshot(svc, new_cid)
                    if final_target_snapshot["stats"] != baseline["stats"]:
                        logger.warning(
                            f"⚠️ 敏感词处理后目标目录规模与基准不一致: "
                            f"{final_target_snapshot['stats']} != {baseline['stats']}，将跳过卡控继续生成分享链接"
                        )
                except Exception as snap_ex:
                    logger.warning(
                        f"⚠️ 敏感词处理后获取目标目录快照失败: {snap_ex}，跳过快照强校验，继续正常生成分享链接及后续步骤。"
                    )

                # 9. 对复制出来的新文件夹生成永久分享链接
                logger.info(f"🔗 正在为 {new_folder_name} (CID: {new_cid}) 创建分享链接...")
                share_link = await svc._share_fids_direct([new_cid])
                if not share_link:
                    raise RuntimeError("创建分享链接失败")

                logger.info(f"✅ 生成定时分享链接成功: {share_link}")
            
            share_review_pending = await _check_scheduled_share_status(svc, share_link)

            # 格式化顶级项结构信息（限 15 项）
            max_show = 15
            items_sorted = sorted(items, key=lambda x: (not x["is_dir"], x["name"].lower()))
            shown_items = items_sorted[:max_show]
            lines = []
            for item in shown_items:
                icon = "📁" if item["is_dir"] else "📄"
                lines.append(f"  {icon} {item['name']}")
            if len(items_sorted) > max_show:
                lines.append(f"  ...等共 {len(items_sorted)} 个项目")

            items_str = "\n".join(lines)
            full_text = f"📁 定时分享: {folder_name}\n"
            if items_str:
                full_text += f"📂 包含项目:\n{items_str}\n"
            full_text += f"🔗 链接: {share_link}"

            metadata = {
                "full_text": full_text,
                "entities": [],
                "photo_id": None
            }

            from app.services.tg_bot import tg_service
            if share_review_pending:
                review_metadata = {
                    **metadata,
                    "scheduled_share_review": {
                        "task_id": task_id,
                        "account_id": task_db.account_id,
                        "share_mode": share_mode,
                        "cleanup_cid": new_cid if share_mode != "direct" else None,
                        "cleanup_folder_name": new_folder_name if share_mode != "direct" else None,
                        "cleanup_enabled": bool(getattr(task_db, "cleanup_temp_dir", True)),
                        "target_channels": task_db.target_channels or [],
                    },
                }
                await tg_service.enqueue_scheduled_share_review(share_link, review_metadata)
                logger.info(f"🔍 分享链接审核中，已加入持久轮询队列: {share_link}")
            elif task_db.target_channels:
                logger.info(f"📢 正在推送至频道: {task_db.target_channels}")
                await tg_service.broadcast_to_channels(
                    {share_link: share_link},
                    metadata,
                    channel_ids=task_db.target_channels
                )
                
            # 11. 按任务开关清理临时目录，并清空该账号的整个回收站
            cleanup_temp_dir = bool(getattr(task_db, "cleanup_temp_dir", True))
            if share_mode != "direct" and not share_review_pending:
                await _cleanup_scheduled_temp_dir(
                    svc,
                    cid=new_cid,
                    folder_name=new_folder_name,
                    enabled=cleanup_temp_dir,
                )
            
            # 12. 更新任务状态为成功
            if not share_review_pending:
                async with async_session() as session:
                    task_db_succ = await session.get(ScheduledShareTask, task_id)
                    if task_db_succ:
                        task_db_succ.status = "success"
                        task_db_succ.last_run_at = datetime.utcnow()
                        await session.commit()
                logger.info(f"🎉 定时分享任务 [{task_id}] '{task_db.name}' 执行完毕！")
            else:
                logger.info(f"⏳ 定时分享任务 [{task_id}] 等待分享链接审核通过后完成")
                    
        except Exception as e:
            logger.error(f"❌ 定时分享任务 [{task_id}] 执行失败: {e}", exc_info=True)
            async with async_session() as session:
                task_db_err = await session.get(ScheduledShareTask, task_id)
                if task_db_err:
                    task_db_err.status = f"failed: {str(e)[:100]}"
                    task_db_err.last_run_at = datetime.utcnow()
                    await session.commit()

    # 将任务整个逻辑放入队列中排队顺序执行
    await svc._enqueue_op(f"scheduled_share_{task_id}", _execute_flow)


class CleanupScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(
            job_defaults={
                "misfire_grace_time": 300,
                "coalesce": True,
                "max_instances": 1,
            }
        )

    def start(self):
        """Start the scheduler"""
        self.update_cleanup_dir_job()
        self.update_cleanup_trash_job()
        self.update_cleanup_capacity_job()
        self.update_session_check_job()
        self.scheduler.start()
        logger.info("[TIME] 定时清理任务已启动")

    def update_cleanup_dir_job(self):
        """Update or remove the cleanup save directory job based on config"""
        job_id = "cleanup_save_dir"
        if settings.P115_CLEANUP_DIR_CRON:
            try:
                self.scheduler.add_job(
                    _scheduled_cleanup_save_dir,
                    CronTrigger.from_crontab(settings.P115_CLEANUP_DIR_CRON),
                    id=job_id,
                    name="清理保存目录",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置清理保存目录定时任务: {settings.P115_CLEANUP_DIR_CRON}")
            except Exception as e:
                logger.error(f"[ERROR] 设置清理保存目录定时任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除清理保存目录定时任务")

    def update_cleanup_trash_job(self):
        """Update or remove the cleanup recycle bin job based on config"""
        job_id = "cleanup_recycle_bin"
        if settings.P115_CLEANUP_TRASH_CRON:
            try:
                self.scheduler.add_job(
                    _scheduled_cleanup_recycle_bin,
                    CronTrigger.from_crontab(settings.P115_CLEANUP_TRASH_CRON),
                    id=job_id,
                    name="清空回收站",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置清空回收站定时任务: {settings.P115_CLEANUP_TRASH_CRON}")
            except Exception as e:
                logger.error(f"[ERROR] 设置清空回收站定时任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除清空回收站定时任务")

    def update_cleanup_capacity_job(self):
        """Update or remove the capacity check job based on config"""
        job_id = "cleanup_capacity_check"
        if settings.P115_CLEANUP_CAPACITY_ENABLED:
            try:
                # 每 30 分钟检查一次容量
                self.scheduler.add_job(
                    _scheduled_capacity_check,
                    'interval',
                    minutes=30,
                    id=job_id,
                    name="自动检测网盘容量",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置容量自动检测任务: 每 30 分钟一次 (阈值: {settings.P115_CLEANUP_CAPACITY_LIMIT} TB)")
            except Exception as e:
                logger.error(f"[ERROR] 设置容量自动检测任务失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("[-] 已移除容量自动检测任务")

    def update_session_check_job(self):
        """Update or start the session check job"""
        job_id = "session_expiry_check"
        try:
            # 每 30 分钟检测一次账号登录 Session 有效性
            self.scheduler.add_job(
                _scheduled_session_expiry_check,
                'interval',
                minutes=30,
                id=job_id,
                name="自动检测115账号登录有效性",
                replace_existing=True
            )
            logger.info("[OK] 已设置115账号登录有效性自动检测任务: 每 30 分钟一次")
        except Exception as e:
            logger.error(f"[ERROR] 设置115账号登录有效性自动检测任务失败: {e}")

    def sync_scheduled_share_job(self, task):
        """同步单个定时分享任务"""
        job_id = f"scheduled_share_{task.id}"
        if task.enabled:
            try:
                self.scheduler.add_job(
                    _run_scheduled_share_task,
                    CronTrigger.from_crontab(task.cron_expression),
                    args=[task.id],
                    id=job_id,
                    name=f"定时分享: {task.name}",
                    replace_existing=True
                )
                logger.info(f"[OK] 已设置定时分享任务 [{task.id}] '{task.name}': {task.cron_expression}")
            except Exception as e:
                logger.error(f"[ERROR] 设置定时分享任务 [{task.id}] 调度失败: {e}")
        else:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info(f"[-] 已移除定时分享任务 [{task.id}] '{task.name}'")

    async def sync_all_scheduled_share_jobs(self):
        """加载并同步所有启用的定时分享任务"""
        try:
            async with async_session() as session:
                result = await session.execute(select(ScheduledShareTask))
                tasks = result.scalars().all()
                for task in tasks:
                    self.sync_scheduled_share_job(task)
            logger.info("[TIME] 所有定时分享任务同步完成")
        except Exception as e:
            logger.error(f"[ERROR] 同步定时分享任务失败: {e}")

    def shutdown(self):
        """Shutdown the scheduler"""
        self.scheduler.shutdown()
        logger.info("[TIME] 定时清理任务已停止")



cleanup_scheduler = CleanupScheduler()
