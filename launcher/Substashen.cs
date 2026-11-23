// Substashen.exe: a tiny launcher so Windows shows "Substashen" (name, icon, publisher) in Settings > Apps > Startup
// and Task Manager, instead of "Python". It starts substashen.pyw from its own folder with pythonw and exits.
// Build: launcher\build.bat
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Substashen")]
[assembly: AssemblyProduct("Substashen")]
[assembly: AssemblyCompany("Substashen")]
[assembly: AssemblyDescription("Substashen")]
[assembly: AssemblyCopyright("")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Launcher
{
    static int Main()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(dir, "substashen.pyw");
        // The Python install manager's pythonw.exe survives version upgrades; otherwise rely on PATH.
        string pyw = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                  @"Python\bin\pythonw.exe");
        if (!File.Exists(pyw)) pyw = "pythonw.exe";
        try
        {
            if (!File.Exists(script)) throw new FileNotFoundException("substashen.pyw is missing next to Substashen.exe", script);
            var psi = new ProcessStartInfo(pyw, "\"" + script + "\"") { WorkingDirectory = dir, UseShellExecute = false };
            Process.Start(psi);
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("Substashen could not start:\n\n" + e.Message + "\n\nPython: " + pyw,
                            "Substashen", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
