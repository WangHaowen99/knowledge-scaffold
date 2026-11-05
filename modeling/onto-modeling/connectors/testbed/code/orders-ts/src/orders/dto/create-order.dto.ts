import { Type } from 'class-transformer';
import {
  ArrayMinSize,
  IsEmail,
  IsInt,
  IsOptional,
  IsPositive,
  Matches,
  Max,
  Min,
  ValidateNested,
} from 'class-validator';

export class CreateOrderItemDto {
  @Matches(/^[A-Z]{3}-\d{4}$/)
  sku: string;

  @IsInt()
  @Min(1)
  @Max(999)
  quantity: number;
}

/** 下单请求 */
export class CreateOrderDto {
  @IsInt()
  @IsPositive()
  customerId: number;

  @ValidateNested({ each: true })
  @Type(() => CreateOrderItemDto)
  @ArrayMinSize(1)
  items: CreateOrderItemDto[];

  @IsOptional()
  @IsEmail()
  notifyEmail?: string;
}
