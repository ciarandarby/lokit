using System.Text;

namespace Lokit.Office.Core;

public static class TextLimits
{
    public static bool ExceedsUnicodeScalarLimit(string text, int maximum)
    {
        if (maximum < 0)
        {
            return true;
        }

        var count = 0;
        foreach (var _ in text.EnumerateRunes())
        {
            if (count >= maximum)
            {
                return true;
            }
            count += 1;
        }
        return false;
    }
}
