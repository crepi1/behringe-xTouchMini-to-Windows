' Startet Midi2Sonar ohne Konsolenfenster (nur fuer den Start aus dem Quellcode;
' die exe braucht das nicht). Fuer Autostart besser die Checkbox in der App nutzen.
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run "pythonw Midi2Sonar.py", 0, False
