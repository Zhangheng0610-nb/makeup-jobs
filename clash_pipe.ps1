$ErrorActionPreference = 'Stop'
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'verge-mihomo', [System.IO.Pipes.PipeDirection]::InOut)
try { $pipe.Connect(3000) } catch { Write-Output "PIPE-FAIL: $($_.Exception.Message)"; exit 1 }
$writer = New-Object System.IO.StreamWriter($pipe)
$writer.AutoFlush = $true
$writer.Write("GET /proxies HTTP/1.1`r`nHost: localhost`r`nAuthorization: Bearer set-your-secret`r`n`r`n")
Start-Sleep -Milliseconds 800
$buf = New-Object byte[] 262144
$ms = New-Object System.IO.MemoryStream
while ($true) {
    $task = $pipe.ReadAsync($buf, 0, 262144)
    if (-not $task.Wait(2000)) { break }
    $n = $task.Result
    if ($n -le 0) { break }
    $ms.Write($buf, 0, $n)
}
$txt = [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
$idx = $txt.IndexOf("`r`n`r`n")
if ($idx -ge 0) { $txt = $txt.Substring($idx + 4) }
Write-Output $txt
