namespace FraudDetection;

public class FraudDetectionService
{
    public bool IsSuspicious(string transactionId, decimal amount, string country)
    {
        Console.WriteLine($"[FraudDetection] Analysing transaction {transactionId} for {country}");
        return amount > 10000;
    }
}
