namespace Refund;

public class RefundService
{
    public string ProcessRefund(string transactionId, decimal amount)
    {
        Console.WriteLine($"[Refund] Refund of {amount} initiated for transaction {transactionId}");
        return $"REFUND_INITIATED:{transactionId}";
    }
}
