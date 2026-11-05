import enum

from sqlalchemy import CheckConstraint, Column, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base


class OrderStatus(enum.Enum):
    """订单状态"""

    CREATED = "created"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


class Customer(Base):
    """客户"""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="客户名称")
    email: Mapped[str | None] = mapped_column(String(128), unique=True)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    """订单"""

    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_amount >= 0", name="ck_order_amount"),)

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.CREATED)
    total_amount = Column(Numeric(12, 2), nullable=False, comment="订单金额")
    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    @validates("total_amount")
    def check_amount(self, key, value):
        if value < 0:
            raise ValueError("金额不能为负")
        return value


class OrderItem(Base):
    """订单明细"""

    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    sku = Column(String(16), nullable=False)
    quantity = Column(Integer, nullable=False)
    order = relationship("Order", back_populates="items")
