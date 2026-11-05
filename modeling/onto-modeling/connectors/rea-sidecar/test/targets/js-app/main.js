const { app, ipcMain, BrowserWindow } = require('electron');
const path = require('path');
const { OrderService } = require('./src/orders');
const customers = require('./src/customers');

const service = new OrderService();

ipcMain.handle('order:create', (_event, input) => service.create(input));
ipcMain.handle('order:approve', (_event, id) => service.approve(id));
ipcMain.handle('customer:list', () => customers.listCustomers());
ipcMain.on('app:quit', () => app.quit());

function createWindow() {
  const win = new BrowserWindow({ webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true } });
  win.loadFile('renderer/index.html');
}

app.whenReady().then(createWindow);
