Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""E:\ComfyUI\raven-image-deploy\deploy.ps1""", 0, True
