namespace PaymentGateway;

public class PaymentGatewayService
{
    public string ProcessPayment(string transactionId, decimal amount, string currency)
    {
        Console.WriteLine($"[PaymentGateway] Processing {currency} {amount} for transaction {transactionId}");
        return $"APPROVED:{transactionId}";
    }
}
