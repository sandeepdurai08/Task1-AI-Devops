namespace Reporting;

public class ReportingService
{
    public string GenerateReport(string reportType, DateTime from, DateTime to)
    {
        Console.WriteLine($"[Reporting] Generating {reportType} report from {from:d} to {to:d}");
        return $"{reportType}_REPORT_{from:yyyyMMdd}_{to:yyyyMMdd}.pdf";
    }
}
