const axios = require('axios');

class Customer {
  constructor(id, name) {
    this.id = id;
    this.name = name;
  }
}

async function listCustomers() {
  const response = await axios.get('https://crm.example.com/api/customers?page=1');
  return response.data.map((row) => new Customer(row.id, row.name));
}

function deleteCustomer(id) {
  return axios.delete('https://crm.example.com/api/customers/' + id);
}

module.exports = { Customer, listCustomers, deleteCustomer };
