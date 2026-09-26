namespace Audit;

public class AuditService
{
    public void LogEvent(string eventType, string userId, string details)
    {
        Console.WriteLine($"[Audit] {DateTime.UtcNow:u} | {eventType} | User: {userId} | {details}");
    }
}
