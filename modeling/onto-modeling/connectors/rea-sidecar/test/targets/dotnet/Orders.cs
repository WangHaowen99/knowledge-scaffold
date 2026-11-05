using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace Sample.Orders
{
    public enum OrderStatus { Draft, Approved, Cancelled }

    public interface IOrderRepository
    {
        Order Find(int id);
        void Save(Order order);
    }

    public class Order
    {
        public int Id { get; set; }
        public int CustomerId { get; set; }
        public decimal Amount { get; set; }
        public OrderStatus Status { get; private set; }
        internal string note;

        public void Approve() { Status = OrderStatus.Approved; }
        public void Cancel(string reason) { Status = OrderStatus.Cancelled; note = reason; }
    }

    public class UrgentOrder : Order
    {
        public int Priority { get; set; }
    }

    public class OrderService
    {
        public const string BaseUrl = "https://erp.example.com/api/orders";
        private readonly IOrderRepository repository;
        private readonly List<Order> pending = new List<Order>();

        public OrderService(IOrderRepository repository) { this.repository = repository; }

        public Order CreateOrder(int customerId, decimal amount)
        {
            var order = new Order { CustomerId = customerId, Amount = amount };
            repository.Save(order);
            pending.Add(order);
            return order;
        }

        public bool ApproveOrder(int id)
        {
            var order = repository.Find(id);
            if (order == null) return false;
            order.Approve();
            NativeMethods.Beep(750, 100);
            return true;
        }

        public string ApproveUrl(int id)
        {
            return BaseUrl + "/" + id + "/approve";
        }

        public void Cancel(int id) { Cancel(id, "none"); }

        public void Cancel(int id, string reason)
        {
            var order = repository.Find(id);
            if (order != null) order.Cancel(reason);
        }
    }

    internal static class NativeMethods
    {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        internal static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint mode, uint flags, IntPtr template);

        [DllImport("kernel32.dll")]
        internal static extern bool Beep(uint frequency, uint duration);

        [DllImport("user32.dll", EntryPoint = "MessageBoxW", CharSet = CharSet.Unicode)]
        internal static extern int ShowMessage(IntPtr owner, string text, string caption, uint type);
    }
}
