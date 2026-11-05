class Customer:
    def __init__(self, customer_id, name):
        self.id = customer_id
        self.name = name


def format_amount(value):
    return f"{value:.2f}"
