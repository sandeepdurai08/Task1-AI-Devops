namespace Reconciliation;

public class ReconciliationService
{
    public bool Reconcile(string batchId, decimal expected, decimal actual)
    {
        Console.WriteLine($"[Reconciliation] Reconciling batch {batchId}: expected={expected}, actual={actual}");
        return expected == actual;
    }
}
