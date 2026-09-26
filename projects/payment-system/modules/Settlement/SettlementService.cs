namespace Settlement;

public class SettlementService
{
    public string SettleBatch(string batchId, decimal totalAmount)
    {
        Console.WriteLine($"[Settlement] Settling batch {batchId} for total {totalAmount}");
        return $"SETTLED:{batchId}";
    }
}
