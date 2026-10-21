'
' Arranca iniciar_bot.bat MINIMIZADO, para dejarlo en la carpeta Inicio de Windows:
'   Win+R -> shell:startup -> pegar ahi un ACCESO DIRECTO a este archivo.
' (Un acceso directo, no una copia: el script busca el .bat en su propia carpeta.)
'
' Minimizado (7) y no oculto (0) a proposito: si el bot falla al arrancar, el
' .bat hace "pause" y la ventana queda en la barra de tareas para leer el error.
' Oculta, quedaria un proceso esperando una tecla que nadie ve.
Option Explicit
Dim fso, sh, carpeta, bat
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
carpeta = fso.GetParentFolderName(WScript.ScriptFullName)
bat = fso.BuildPath(carpeta, "iniciar_bot.bat")
If Not fso.FileExists(bat) Then
  MsgBox "No encuentro " & bat, vbExclamation, "Bot de fallas"
  WScript.Quit 1
End If
sh.CurrentDirectory = carpeta
sh.Run "cmd /c """ & bat & """", 7, False
