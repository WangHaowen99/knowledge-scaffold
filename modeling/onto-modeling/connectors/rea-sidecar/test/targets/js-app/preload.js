const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  createOrder: (input) => ipcRenderer.invoke('order:create', input),
  approveOrder: (id) => ipcRenderer.invoke('order:approve', id),
  listCustomers: () => ipcRenderer.invoke('customer:list'),
  quit: () => ipcRenderer.send('app:quit'),
});
