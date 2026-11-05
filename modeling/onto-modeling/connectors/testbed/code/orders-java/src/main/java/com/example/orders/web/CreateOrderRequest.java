package com.example.orders.web;

import java.util.List;
import javax.validation.Valid;
import javax.validation.constraints.NotBlank;
import javax.validation.constraints.NotEmpty;
import javax.validation.constraints.NotNull;
import javax.validation.constraints.Positive;
import javax.validation.constraints.Size;

public class CreateOrderRequest {

    @NotNull
    private Long customerId;

    @Valid
    @NotEmpty
    @Size(max = 50)
    private List<ItemLine> items;

    public static class ItemLine {
        @NotBlank
        private String sku;

        @Positive
        private int quantity;
    }
}
