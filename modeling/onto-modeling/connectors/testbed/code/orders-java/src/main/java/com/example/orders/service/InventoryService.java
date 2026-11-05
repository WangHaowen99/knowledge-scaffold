package com.example.orders.service;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/** 库存 */
@Service
public class InventoryService {

    private final JdbcTemplate jdbcTemplate;

    public InventoryService(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public boolean reserve(String sku, int quantity) {
        int updated = jdbcTemplate.update(
                "UPDATE products SET stock = stock - ? WHERE sku = ? AND stock >= ?", quantity, sku, quantity);
        return updated == 1;
    }
}
