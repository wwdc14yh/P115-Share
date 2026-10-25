"""
定时分享任务管理 API
"""
import asyncio

from app.api.auth import get_current_user
from app.core.database import async_session
from app.models.schema import ScheduledShareTask
from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update

router = APIRouter(prefix="/scheduled-share", tags=["scheduled-share"])
_current_user_dependency = Depends(get_current_user)


class ScheduledShareTaskCreate(BaseModel):
    name: str
    account_id: int
    dir_path: str
    cron_expression: str
    clear_files: bool = True
    share_mode: str = "move"
    cleanup_temp_dir: bool = True
    min_size: float = 0.0
    min_size_unit: str = "GB"
    target_channels: list[str] = []
    enabled: bool = True
    sensitive_replace_enabled: bool = False
    sensitive_replace_pinyin: str | bool | int = "0"
    sensitive_replace_tmdb: bool = False

    @field_validator("share_mode")
    @classmethod
    def validate_share_mode(cls, value):
        if value not in {"move", "copy", "direct", "sunflower", "per_file"}:
            raise ValueError("share_mode 必须是 move、copy、direct、sunflower 或 per_file")
        return value

    @field_validator('min_size', mode='before')
    @classmethod
    def coerce_min_size(cls, v):
        if v is None or v == "":
            return 0.0
        return v


class ScheduledShareTaskUpdate(BaseModel):
    name: str | None = None
    account_id: int | None = None
    dir_path: str | None = None
    cron_expression: str | None = None
    clear_files: bool | None = None
    share_mode: str | None = None
    cleanup_temp_dir: bool | None = None
    min_size: float | None = None
    min_size_unit: str | None = None
    target_channels: list[str] | None = None
    enabled: bool | None = None
    sensitive_replace_enabled: bool | None = None
    sensitive_replace_pinyin: str | bool | int | None = None
    sensitive_replace_tmdb: bool | None = None

    @field_validator("share_mode")
    @classmethod
    def validate_share_mode(cls, value):
        if value is not None and value not in {"move", "copy", "direct", "sunflower", "per_file"}:
            raise ValueError("share_mode 必须是 move、copy、direct、sunflower 或 per_file")
        return value

    @field_validator('min_size', mode='before')
    @classmethod
    def coerce_min_size(cls, v):
        if v is None or v == "":
            return 0.0
        return v


@router.get("/")
async def list_tasks(user=_current_user_dependency):
    """获取所有定时分享任务列表"""
    async with async_session() as session:
        result = await session.execute(select(ScheduledShareTask).order_by(ScheduledShareTask.id.desc()))
        tasks = result.scalars().all()
        return {
            "state": True,
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "account_id": t.account_id,
                    "dir_path": t.dir_path,
                    "cron_expression": t.cron_expression,
                    "clear_files": t.clear_files,
                    "share_mode": t.share_mode or ("move" if t.clear_files else "copy"),
                    "cleanup_temp_dir": t.cleanup_temp_dir,
                    "min_size": t.min_size,
                    "min_size_unit": t.min_size_unit,
                    "target_channels": t.target_channels,
                    "enabled": t.enabled,
                    "status": t.status,
                    "last_run_at": (t.last_run_at.isoformat() + "Z") if t.last_run_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "sensitive_replace_enabled": t.sensitive_replace_enabled,
                    "sensitive_replace_pinyin": t.sensitive_replace_pinyin,
                    "sensitive_replace_tmdb": t.sensitive_replace_tmdb,
                }
                for t in tasks
            ]
        }


@router.post("/")
async def create_task(data: ScheduledShareTaskCreate, user=_current_user_dependency):
    """创建定时分享任务"""
    # 校验 cron 表达式
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(data.cron_expression)
    except Exception as e:  # noqa: BLE001
        return {"state": False, "message": f"Cron 表达式格式错误: {e}"}

    # Reconcile share_mode and clear_files:
    share_mode = data.share_mode
    clear_files = data.clear_files
    if share_mode == "sunflower" and not data.target_channels:
        return {"state": False, "message": "葵花宝典模式必须选择至少一个推送频道"}
    if share_mode == "per_file" and not data.target_channels:
        return {"state": False, "message": "逐条分享模式必须选择至少一个推送频道"}
    if share_mode == "move" and not clear_files:
        share_mode = "copy"
    elif share_mode == "direct" or share_mode in {"copy", "sunflower", "per_file"}:
        clear_files = False
    elif share_mode == "move":
        clear_files = True

    async with async_session() as session:
        task = ScheduledShareTask(
            name=data.name,
            account_id=data.account_id,
            dir_path=data.dir_path,
            cron_expression=data.cron_expression,
            clear_files=clear_files,
            share_mode=share_mode,
            cleanup_temp_dir=data.cleanup_temp_dir,
            min_size=data.min_size,
            min_size_unit=data.min_size_unit,
            target_channels=data.target_channels,
            enabled=data.enabled,
            status="waiting" if data.enabled else "disabled",
            sensitive_replace_enabled=data.sensitive_replace_enabled,
            sensitive_replace_pinyin=str(data.sensitive_replace_pinyin) if data.sensitive_replace_pinyin is not None else "0",
            sensitive_replace_tmdb=data.sensitive_replace_tmdb
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": "创建定时任务成功", "data": {"id": task.id}}


@router.put("/{task_id}")
async def update_task(task_id: int, data: ScheduledShareTaskUpdate, user=_current_user_dependency):
    """更新定时分享任务"""
    if data.cron_expression is not None:
        from apscheduler.triggers.cron import CronTrigger
        try:
            CronTrigger.from_crontab(data.cron_expression)
        except Exception as e:  # noqa: BLE001
            return {"state": False, "message": f"Cron 表达式格式错误: {e}"}

    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        # 更新字段
        update_dict = data.model_dump(exclude_unset=True)
        if "sensitive_replace_pinyin" in update_dict and update_dict["sensitive_replace_pinyin"] is not None:
            update_dict["sensitive_replace_pinyin"] = str(update_dict["sensitive_replace_pinyin"])
        resulting_share_mode = update_dict.get(
            "share_mode", task.share_mode or ("move" if task.clear_files else "copy")
        )
        resulting_channels = update_dict.get("target_channels", task.target_channels)
        if resulting_share_mode == "sunflower" and not resulting_channels:
            return {"state": False, "message": "葵花宝典模式必须选择至少一个推送频道"}
        if resulting_share_mode == "per_file" and not resulting_channels:
            return {"state": False, "message": "逐条分享模式必须选择至少一个推送频道"}
        # Reconcile share_mode and clear_files if either is updated
        if "share_mode" in update_dict or "clear_files" in update_dict:
            curr_share_mode = update_dict.get("share_mode", task.share_mode or ("move" if task.clear_files else "copy"))
            curr_clear_files = update_dict.get("clear_files", task.clear_files)
            
            if curr_share_mode == "move" and not curr_clear_files:
                curr_share_mode = "copy"
            elif curr_share_mode == "direct" or curr_share_mode in {"copy", "sunflower", "per_file"}:
                curr_clear_files = False
            elif curr_share_mode == "move":
                curr_clear_files = True
                
            update_dict["share_mode"] = curr_share_mode
            update_dict["clear_files"] = curr_clear_files

        for field, val in update_dict.items():
            setattr(task, field, val)

        if data.enabled is not None:
            task.status = "waiting" if task.enabled else "disabled"

        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": "更新定时任务成功"}


@router.delete("/{task_id}")
async def delete_task(task_id: int, user=_current_user_dependency):
    """删除定时分享任务"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        # 禁用并从调度器中移除
        task.enabled = False
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        await session.delete(task)
        await session.commit()

        return {"state": True, "message": "删除定时任务成功"}


@router.post("/{task_id}/toggle")
async def toggle_task(task_id: int, user=_current_user_dependency):
    """启用/禁用定时任务"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        task.enabled = not task.enabled
        task.status = "waiting" if task.enabled else "disabled"

        await session.commit()
        await session.refresh(task)

        # 同步调度器中的任务
        from app.services.scheduler import cleanup_scheduler
        cleanup_scheduler.sync_scheduled_share_job(task)

        return {"state": True, "message": f"任务已{'启用' if task.enabled else '禁用'}", "data": {
            "id": task.id,
            "enabled": task.enabled,
            "status": task.status
        }}


@router.post("/{task_id}/trigger")
async def trigger_task(task_id: int, user=_current_user_dependency):
    """手动触发定时分享任务（后台异步执行）"""
    async with async_session() as session:
        task = await session.get(ScheduledShareTask, task_id)
        if not task:
            return {"state": False, "message": "任务不存在"}

        if not task.enabled:
            return {"state": False, "message": "任务未启用，无法触发"}

        if task.status == "running":
            return {"state": False, "message": "任务正在运行中，请等待当前任务执行完成后再触发"}

        # Claim the task before returning so concurrent requests cannot queue duplicate runs.
        result = await session.execute(
            update(ScheduledShareTask)
            .where(
                ScheduledShareTask.id == task_id,
                ScheduledShareTask.enabled.is_(True),
                ScheduledShareTask.status != "running",
            )
            .values(status="running")
        )
        if result.rowcount != 1:
            await session.refresh(task)
            if task.status == "running":
                return {"state": False, "message": "任务正在运行中，请等待当前任务执行完成后再触发"}
            if not task.enabled:
                return {"state": False, "message": "任务未启用，无法触发"}
            return {"state": False, "message": "任务状态已变更，请稍后重试"}

        await session.commit()

        # 异步启动任务
        from app.services.scheduler import _run_scheduled_share_task
        asyncio.create_task(_run_scheduled_share_task(task_id, already_claimed=True))

        return {"state": True, "message": "任务已手动触发，后台异步执行中"}
