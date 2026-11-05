import { Body, Controller, Get, Param, Post } from '@nestjs/common';
import { CreateOrderDto } from './dto/create-order.dto';
import { OrdersService } from './orders.service';

@Controller('api/orders')
export class OrdersController {
  constructor(private readonly ordersService: OrdersService) {}

  @Post()
  create(@Body() dto: CreateOrderDto) {
    return this.ordersService.submit(dto);
  }

  @Get(':id')
  findOne(@Param('id') id: string) {
    return { id };
  }

  @Post(':id/cancel')
  cancel(@Param('id') id: string) {
    return this.ordersService.cancel(id);
  }

  @Get('reports/totals')
  totals() {
    return this.ordersService.totalsByCustomer();
  }
}
