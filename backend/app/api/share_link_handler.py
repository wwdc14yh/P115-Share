from fastapi import APIRouter, Depends
from app.api.auth import get_current_user

router = APIRouter(prefix="/sharelinkhandler", tags=["sharelinkhandler"])

CURRENT_USER_DEPENDENCY = Depends(get_current_user)

@router.get("/{share_link}")
async def sharelink_handler(share_link: str, account_id: int | None = None, current_user=CURRENT_USER_DEPENDENCY):
    """
    Handle the share link and return the appropriate response.
    Args:
        share_link (str): The share link to be handled.
        account_id (int | None): 可选账号ID，用于指定使用哪个账号处理分享链接。如果未提供，则使用默认账号。
        current_user: The current authenticated user.
    """
    
    return {"state": True, "message": f"Handled share link: {share_link}"}