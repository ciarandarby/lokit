namespace Lokit.Office.Core;

public class OfficeException : Exception
{
    public OfficeException(string message) : base(message) { }
    public OfficeException(string message, Exception innerException) : base(message, innerException) { }
}

public sealed class OfficePackageException : OfficeException
{
    public OfficePackageException(string message) : base(message) { }
    public OfficePackageException(string message, Exception innerException) : base(message, innerException) { }
}

public sealed class OfficeUnsupportedPackageException : OfficeException
{
    public OfficeUnsupportedPackageException(string message) : base(message) { }
}

public sealed class OfficeReinsertionException : OfficeException
{
    public OfficeReinsertionException(string message) : base(message) { }
}
