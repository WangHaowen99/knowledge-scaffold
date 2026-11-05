import { Injectable } from '@nestjs/common';
import { DataSource } from 'typeorm';

/** 库存 */
@Injectable()
export class InventoryService {
  constructor(private readonly dataSource: DataSource) {}

  async reserve(sku: string, quantity: number): Promise<boolean> {
    const result = await this.dataSource.query(
      'UPDATE products SET stock = stock - $1 WHERE sku = $2 AND stock >= $1',
      [quantity, sku],
    );
    return result[1] === 1;
  }
}
