package com.example.orders.web;

import com.example.orders.domain.Order;
import com.example.orders.service.OrderService;
import java.util.List;
import javax.validation.Valid;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping
    public Order create(@Valid @RequestBody CreateOrderRequest request) {
        return orderService.submit(request.getCustomerId(), null);
    }

    @PostMapping("/{id}/cancel")
    public void cancel(@PathVariable Long id) {
        orderService.cancel(id);
    }

    @GetMapping("/paid")
    public List<Order> paid(@RequestParam Long customerId) {
        return orderService.paidOrders(customerId);
    }

    @RequestMapping(value = "/search", method = RequestMethod.GET)
    public List<Order> search(@RequestParam String keyword) {
        return List.of();
    }

    @GetMapping(Paths.STATS)
    public String stats() {
        return "";
    }
}
