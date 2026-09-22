namespace MathLibrary
{
    /// <summary>
    /// Simple math helper - demonstrates a DLL class library.
    /// </summary>
    public class MathHelper
    {
        public int Add(int a, int b) => a + b;

        public int Subtract(int a, int b) => a - b;

        public double Multiply(double a, double b) => a * b;

        public double Divide(double a, double b)
        {
            if (b == 0) throw new System.DivideByZeroException("Divisor cannot be zero.");
            return a / b;
        }
    }
}
