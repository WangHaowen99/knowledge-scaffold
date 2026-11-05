package com.example.orders.service;

import com.example.orders.domain.Order;
import com.example.orders.domain.OrderItem;
import com.example.orders.domain.OrderStatus;
import com.example.orders.repository.OrderRepository;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 下单与取消 */
@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final InventoryService inventoryService;

    public OrderService(OrderRepository orderRepository, InventoryService inventoryService) {
        this.orderRepository = orderRepository;
        this.inventoryService = inventoryService;
    }

    /** 提交订单 */
    @Transactional
    public Order submit(Long customerId, List<OrderItem> items) {
        for (OrderItem item : items) {
            inventoryService.reserve("SKU", 1);
        }
        return orderRepository.save(new Order());
    }

    public void cancel(Long orderId) {
        Order order = orderRepository.findById(orderId).orElseThrow();
        order.cancel();
    }

    public List<Order> paidOrders(Long customerId) {
        return orderRepository.findByCustomerAndStatus(customerId, OrderStatus.PAID);
    }

    private void audit(Order order) {
    }
}
