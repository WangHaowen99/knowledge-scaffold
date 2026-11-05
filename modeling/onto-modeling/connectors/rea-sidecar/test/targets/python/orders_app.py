import json
import urllib.request

from orders_lib import Customer, format_amount

BASE_URL = "https://erp.example.com/api"


class Order:
    def __init__(self, customer_id, amount):
        self.customer_id = customer_id
        self.amount = amount
        self.status = "draft"

    def approve(self):
        self.status = "approved"
        return self


class OrderService:
    def create(self, customer_id, amount):
        order = Order(customer_id, amount)
        body = json.dumps(order.__dict__).encode("utf-8")
        request = urllib.request.Request(BASE_URL + "/orders", data=body, method="POST")
        return order, request

    def approve(self, order_id):
        return urllib.request.Request(f"{BASE_URL}/orders/{order_id}/approve", method="POST")


def main():
    service = OrderService()
    order, _ = service.create(Customer(1, "sample").id, 2)
    print(format_amount(order.amount))


if __name__ == "__main__":
    main()
