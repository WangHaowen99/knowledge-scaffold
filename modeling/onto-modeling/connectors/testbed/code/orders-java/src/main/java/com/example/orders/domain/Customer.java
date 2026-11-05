package com.example.orders.domain;

import java.util.List;
import javax.persistence.*;
import javax.validation.constraints.Email;
import javax.validation.constraints.NotBlank;
import javax.validation.constraints.Size;

/** 客户 */
@Entity
@Table(name = "customers")
public class Customer {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** 客户名称 */
    @NotBlank
    @Size(max = 64)
    @Column(name = "full_name", nullable = false, length = 64)
    private String name;

    @Email
    @Column(unique = true)
    private String email;

    @OneToMany(mappedBy = "customer")
    private List<Order> orders;

    public Long getId() {
        return id;
    }
}
