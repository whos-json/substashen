@echo off
rem Rebuilds substashen.ico and Substashen.exe (uses Python + Pillow and the C# compiler that ships with Windows).
cd /d "%~dp0"
python make_icon.py || exit /b 1
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /optimize+ ^
  /win32icon:..\substashen.ico /r:System.Windows.Forms.dll /out:..\Substashen.exe Substashen.cs || exit /b 1
echo Built ..\Substashen.exe
