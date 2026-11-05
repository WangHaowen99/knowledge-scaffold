const BASE = 'https://erp.example.com/api';

class Order {
  constructor(customerId, amount) {
    this.customerId = customerId;
    this.amount = amount;
    this.status = 'draft';
  }

  approve() {
    this.status = 'approved';
    return this;
  }
}

class OrderService {
  async create(input) {
    const order = new Order(input.customerId, input.amount);
    await fetch(`${BASE}/orders`, { method: 'POST', body: JSON.stringify(order) });
    return order;
  }

  async approve(id) {
    if (!id) throw new Error('missing order id');
    return fetch(`${BASE}/orders/${id}/approve`, { method: 'POST' });
  }
}

module.exports = { Order, OrderService };
