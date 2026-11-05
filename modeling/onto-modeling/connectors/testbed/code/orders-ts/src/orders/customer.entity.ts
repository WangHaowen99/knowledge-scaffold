import { Column, Entity, OneToMany, PrimaryGeneratedColumn } from 'typeorm';
import { Order } from './order.entity';

/** 客户 */
@Entity('customers')
export class Customer {
  @PrimaryGeneratedColumn()
  id: number;

  @Column({ name: 'full_name', length: 64, comment: '客户名称' })
  name: string;

  @Column({ unique: true, nullable: true })
  email?: string;

  @OneToMany(() => Order, (order) => order.customer)
  orders: Order[];
}
