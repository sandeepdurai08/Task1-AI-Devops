namespace Auth;

public class AuthService
{
    public bool Authenticate(string apiKey, string secret)
    {
        // [S] Never log credentials — not even partial key fragments
        Console.WriteLine("[Auth] Authentication attempt received.");
        return !string.IsNullOrEmpty(apiKey) && !string.IsNullOrEmpty(secret);
    }
}
