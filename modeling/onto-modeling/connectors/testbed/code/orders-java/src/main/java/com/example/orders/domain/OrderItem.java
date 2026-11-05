package com.example.orders.domain;

import java.math.BigDecimal;
import javax.persistence.*;
import javax.validation.constraints.Max;
import javax.validation.constraints.Min;

/** 订单明细 */
@Entity
@Table(name = "order_items")
public class OrderItem {

    @Id
    @GeneratedValue
    private Long id;

    @ManyToOne
    @JoinColumn(name = "order_id")
    private Order order;

    @ManyToOne
    @JoinColumn(name = "product_id")
    private Product product;

    @Min(1)
    @Max(999)
    private int quantity;

    @Column(name = "unit_price", precision = 12, scale = 2)
    private BigDecimal unitPrice;
}
