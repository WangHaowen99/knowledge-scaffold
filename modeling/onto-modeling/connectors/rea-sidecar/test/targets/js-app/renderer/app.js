const routes = [
  { path: '/orders', view: 'OrderList' },
  { path: '/orders/:id', view: 'OrderDetail' },
  { path: '/customers', view: 'CustomerList' },
];

function navigate(path) {
  history.pushState({}, '', path);
}

document.getElementById('root').addEventListener('click', () => {
  window.api.createOrder({ customerId: 1, amount: 2 });
  navigate('/orders');
});

window.routes = routes;
