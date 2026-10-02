$ErrorActionPreference = "Stop"
$url = "http://127.0.0.1:8765/"
Write-Host "Opening ChatGPT-Actuator Admin UI:"
Write-Host $url
Start-Process $url
