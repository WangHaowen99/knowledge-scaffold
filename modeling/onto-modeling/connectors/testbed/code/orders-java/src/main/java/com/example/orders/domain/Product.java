package com.example.orders.domain;

import java.math.BigDecimal;
import javax.persistence.*;
import javax.validation.constraints.Pattern;

/** 商品 */
@Entity
@Table(name = "products")
public class Product {

    @Id
    private Long id;

    @Pattern(regexp = "[A-Z]{3}-\\d{4}")
    @Column(unique = true, nullable = false)
    private String sku;

    private String title;

    private BigDecimal price;

    private int stock;
}
