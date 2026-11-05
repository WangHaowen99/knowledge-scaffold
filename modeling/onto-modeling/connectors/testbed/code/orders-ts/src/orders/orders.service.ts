import { Injectable, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { InventoryService } from '../inventory/inventory.service';
import { CreateOrderDto } from './dto/create-order.dto';
import { Order } from './order.entity';
import { OrderStatus } from './order-status.enum';

/** 下单与取消 */
@Injectable()
export class OrdersService {
  constructor(
    @InjectRepository(Order) private readonly orders: Repository<Order>,
    private readonly inventory: InventoryService,
  ) {}

  /** 提交订单 */
  async submit(dto: CreateOrderDto): Promise<Order> {
    for (const item of dto.items) {
      await this.inventory.reserve(item.sku, item.quantity);
    }
    return this.orders.save(this.orders.create({ status: OrderStatus.Created }));
  }

  async cancel(id: string): Promise<void> {
    const order = await this.orders.findOneBy({ id });
    if (!order) {
      throw new NotFoundException();
    }
    order.status = OrderStatus.Cancelled;
    await this.orders.save(order);
  }

  async totalsByCustomer(): Promise<Array<{ id: number; total: string }>> {
    return this.orders.query(`
      SELECT c.id, SUM(o.total_amount) AS total
      FROM orders o JOIN customers c ON c.id = o.customer_id
      GROUP BY c.id`);
  }

  private audit(order: Order): void {}
}
