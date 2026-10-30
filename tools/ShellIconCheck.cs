using System;
using System.Runtime.InteropServices;
using System.Drawing;

public class ShellIconCheck
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    public struct SHFILEINFO
    {
        public IntPtr hIcon;
        public int iIcon;
        public uint dwAttributes;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szDisplayName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 80)]
        public string szTypeName;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Auto)]
    public static extern IntPtr SHGetFileInfo(
        string pszPath, uint dwFileAttributes,
        ref SHFILEINFO psfi, uint cbFileInfo, uint uFlags);

    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr hIcon);

    public static void Dump(string path, string outPng)
    {
        const uint SHGFI_ICON = 0x100;
        const uint SHGFI_LARGEICON = 0x0;
        SHFILEINFO shfi = new SHFILEINFO();
        IntPtr res = SHGetFileInfo(path, 0, ref shfi,
            (uint)Marshal.SizeOf(shfi), SHGFI_ICON | SHGFI_LARGEICON);

        if (res == IntPtr.Zero || shfi.hIcon == IntPtr.Zero)
        {
            Console.WriteLine("SHGetFileInfo 失败");
            return;
        }

        using (Icon icon = Icon.FromHandle(shfi.hIcon))
        using (Bitmap bmp = icon.ToBitmap())
        {
            bmp.Save(outPng, System.Drawing.Imaging.ImageFormat.Png);
            int cx = bmp.Width / 2;
            int cy = bmp.Height / 2;
            Color c = bmp.GetPixel(cx, cy);
            Console.WriteLine("Shell 图标尺寸: " + bmp.Width + "x" + bmp.Height);
            Console.WriteLine("中心像素: R=" + c.R + " G=" + c.G + " B=" + c.B);
            bool brownish = c.R > 120 && c.R < 210 && c.G > 80 && c.G < 170
                            && c.B < 130 && c.R > c.B + 40;
            Console.WriteLine(brownish
                ? "判定: 是自定义图标（金棕色）"
                : "判定: 不是自定义图标（灰/默认）");
        }
        DestroyIcon(shfi.hIcon);
    }
}
