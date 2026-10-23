"""
定时跨账户转存文件夹任务管理 API
"""


import asyncio

from app.api.auth import get_current_user
from app.core.database import async_session
from app.models.schema import ScheduledTransferTask
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

router = APIRouter(prefix="/scheduled-transfer", tags=["scheduled-transfer"])
CURRENT_USER_DEPENDENCY = Depends(get_current_user)


class ScheduledTransferTaskCreate(BaseModel):
    name: str
    source_account_id: int
    target_account_id: int
    source_dir_path: str
    target_dir_path: str
    cron_expression: str
    remove_source_after_transfer: bool = True
    share_file_after_transfer: bool = False
    remove_file_after_share: bool = False
    enabled: bool = True


class ScheduledTransferTaskUpdate(BaseModel):
    name: str | None = None
    source_account_id: int | None = None
    target_account_id: int | None = None
    source_dir_path: str | None = None
    target_dir_path: str | None = None
    cron_expression: str | None = None
    remove_source_after_transfer: bool | None = None
    share_file_after_transfer: bool | None = None
    remove_file_after_share: bool | None = None
    enabled: bool | None = None


@router.get("/")
async def list_scheduled_transfer_tasks(current_user=CURRENT_USER_DEPENDENCY):
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledTransferTask).order_by(ScheduledTransferTask.id.desc())
        )
        tasks = result.scalars().all()
        return {"state": True, "tasks": tasks}

@router.post("/create")
async def create_scheduled_transfer_task(data: ScheduledTransferTaskCreate, current_user=CURRENT_USER_DEPENDENCY):
    print("Creating scheduled transfer task with data:", data.model_dump())
    async with async_session() as session:
        new_task = ScheduledTransferTask(**data.model_dump())
        session.add(new_task)
        await session.commit()
        await session.refresh(new_task)
        return {"state": True, "task": new_task}
    
@router.put("/{task_id}")
async def update_scheduled_transfer_task(task_id: int, data: ScheduledTransferTaskUpdate, current_user=CURRENT_USER_DEPENDENCY):
    async with async_session() as session:
        task = await session.get(ScheduledTransferTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(task, key, value)
        await session.commit()
        await session.refresh(task)
        return {"state": True, "task": task}
    
@router.delete("/{task_id}")
async def delete_scheduled_transfer_task(task_id: int, current_user=CURRENT_USER_DEPENDENCY):
    async with async_session() as session:
        task = await session.get(ScheduledTransferTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        await session.delete(task)
        await session.commit()
        return {"state": True, "message": "Task deleted successfully"}
    
@router.post("/{task_id}/toggle")
async def toggle_scheduled_transfer_task(task_id: int, current_user=CURRENT_USER_DEPENDENCY):
    async with async_session() as session:
        task = await session.get(ScheduledTransferTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        task.enabled = not task.enabled
        await session.commit()
        await session.refresh(task)
        return {"state": True, "task": task}
    
@router.post("/{task_id}/run")
async def run_scheduled_transfer_task(task_id: int, current_user=CURRENT_USER_DEPENDENCY):
    async with async_session() as session:
        task = await session.get(ScheduledTransferTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        # Here you would implement the logic to run the transfer task immediately.
        # For now, we'll just return a success message.
        from app.services.transfer import run_transfer_task
        asyncio.create_task(run_transfer_task(task_id))  # Run the transfer task asynchronously
        return {"state": True, "message": "Task executed successfully (simulation)"}