from fastapi import APIRouter, Depends

from app.orders.schemas import OrderCreate
from app.orders.service import InventoryService, OrderService

router = APIRouter(prefix="/api/orders", tags=["orders"])


def get_service() -> OrderService:
    return OrderService(None, InventoryService(None))


@router.post("", status_code=201)
def create_order(payload: OrderCreate, service: OrderService = Depends(get_service)):
    return service.submit(payload)


@router.get("/{order_id}")
def get_order(order_id: int):
    return {"id": order_id}


@router.post("/{order_id}/cancel")
def cancel_order(order_id: int, service: OrderService = Depends(get_service)):
    service.cancel(order_id)
