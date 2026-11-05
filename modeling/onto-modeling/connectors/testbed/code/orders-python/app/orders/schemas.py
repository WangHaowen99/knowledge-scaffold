from pydantic import BaseModel, Field, field_validator


class OrderItemIn(BaseModel):
    sku: str = Field(..., pattern=r"^[A-Z]{3}-\d{4}$", description="商品编码")
    quantity: int = Field(..., ge=1, le=999)


class OrderCreate(BaseModel):
    """下单请求"""

    customer_id: int = Field(..., gt=0)
    items: list[OrderItemIn] = Field(..., min_length=1, max_length=50)
    discount_rate: float = Field(0.0, ge=0.0, le=0.5)

    @field_validator("items")
    @classmethod
    def no_duplicate_sku(cls, items):
        skus = [item.sku for item in items]
        if len(skus) != len(set(skus)):
            raise ValueError("同一商品只能出现一次")
        return items
