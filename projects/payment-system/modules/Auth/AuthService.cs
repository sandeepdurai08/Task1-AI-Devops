namespace Auth;

public class AuthService
{
    public bool Authenticate(string apiKey, string secret)
    {
        Console.WriteLine($"[Auth] Authenticating API key: {apiKey[..4]}****");
        return !string.IsNullOrEmpty(apiKey) && !string.IsNullOrEmpty(secret);
    }
}
