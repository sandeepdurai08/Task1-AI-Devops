namespace Notification;

public class NotificationService
{
    public void SendAlert(string recipient, string message, string channel = "email")
    {
        Console.WriteLine($"[Notification] Sending via {channel} to {recipient}: {message}");
    }
}
