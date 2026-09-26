namespace Billing;

public class BillingService
{
    public decimal GenerateInvoice(string customerId, decimal amount)
    {
        Console.WriteLine($"[Billing] Invoice generated for customer {customerId}: {amount}");
        return amount * 1.18m;
    }
}
