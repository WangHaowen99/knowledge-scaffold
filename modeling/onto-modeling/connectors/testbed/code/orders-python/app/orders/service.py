from sqlalchemy import text

from app.orders.models import Order, OrderStatus
from app.orders.schemas import OrderCreate


class InventoryService:
    """库存"""

    def __init__(self, session):
        self.session = session

    def reserve(self, sku: str, quantity: int) -> bool:
        result = self.session.execute(
            text("UPDATE products SET stock = stock - :qty WHERE sku = :sku AND stock >= :qty"),
            {"qty": quantity, "sku": sku},
        )
        return result.rowcount == 1


class OrderService:
    """下单与取消"""

    def __init__(self, session, inventory: InventoryService):
        self.session = session
        self.inventory = inventory

    def submit(self, payload: OrderCreate) -> Order:
        """提交订单"""
        for item in payload.items:
            self.inventory.reserve(item.sku, item.quantity)
        order = Order(customer_id=payload.customer_id, status=OrderStatus.CREATED, total_amount=0)
        self.session.add(order)
        return order

    def cancel(self, order_id: int) -> None:
        order = self.session.get(Order, order_id)
        order.status = OrderStatus.CANCELLED

    def totals_by_customer(self):
        sql = text(
            "SELECT c.id, SUM(o.total_amount) AS total FROM orders o "
            "JOIN customers c ON c.id = o.customer_id GROUP BY c.id"
        )
        return self.session.execute(sql).all()

    def _audit(self, order):
        pass
