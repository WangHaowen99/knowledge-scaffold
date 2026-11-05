package com.example.orders.repository;

import com.example.orders.domain.Order;
import com.example.orders.domain.OrderStatus;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface OrderRepository extends JpaRepository<Order, Long> {

    @Query("SELECT o FROM Order o WHERE o.customer.id = :customerId AND o.status = :status")
    List<Order> findByCustomerAndStatus(@Param("customerId") Long customerId, @Param("status") OrderStatus status);

    @Query(value = "SELECT c.id, SUM(o.total_amount) FROM orders o "
            + "JOIN customers c ON c.id = o.customer_id GROUP BY c.id", nativeQuery = true)
    List<Object[]> totalsByCustomer();

    List<Order> findByStatus(OrderStatus status);
}
