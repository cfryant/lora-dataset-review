<#
  Windows helper: (re)start one of the review servers as a HIDDEN background process.
  ==================================================================================
  WHAT:  wraps `python <app>/server.py ...` so the user does not need to keep a console open.
         It only ever stops a process that is listening on the port AND is this repo's
         server.py; anything else on that port is left alone and reported.
  WHEN:  on the Windows machine that holds the images/videos (the servers read media locally).
  USAGE: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_review.ps1 -App stills [-Port 8765] [-Stop] [-- server args]
         powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_review.ps1 -App video -ServerArgs '--config','D:\data\review_config.json'
         -Python C:\path\to\python.exe   to pick an interpreter (default: python / py on PATH)
  OUTPUT: prints the URL and PID; logs go to <app>\server.log / server.err.log (gitignored).
  NOTE:  there is no autostart; run it again after a reboot.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('stills', 'video')][string]$App,
  [int]$Port = 0,
  [switch]$Stop,
  [string]$Python = '',
  [string[]]$ServerArgs = @()
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dir = Join-Path $repo ($(if ($App -eq 'stills') { 'stills_review' } else { 'video_review' }))
$server = Join-Path $dir 'server.py'
if ($Port -eq 0) { $Port = $(if ($App -eq 'stills') { 8765 } else { 8766 }) }

function Get-Listener { Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 }

# Stop an existing instance (only if it is this repo's server.py)
$l = Get-Listener
if ($l) {
  $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($l.OwningProcess)").CommandLine
  if ($cmd -notlike "*$server*" -and $cmd -notlike "*$(Split-Path -Leaf $dir)*server.py*") {
    Write-Output "Port $Port is used by another program (PID $($l.OwningProcess)) - not touching it. Pick another -Port."; exit 1
  }
  Stop-Process -Id $l.OwningProcess -Force
  Write-Output "Stopped previous $App server (PID $($l.OwningProcess))"
  Start-Sleep -Milliseconds 500
}
if ($Stop) { exit 0 }

if (-not $Python) {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if (-not $c) { $c = Get-Command py -ErrorAction SilentlyContinue }
  if (-not $c) { throw 'No Python found on PATH; pass -Python C:\path\to\python.exe' }
  $Python = $c.Source
}
$argList = @('-u', "`"$server`"", '--port', $Port) + ($ServerArgs | ForEach-Object { "`"$_`"" })
$p = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $dir 'server.log') -RedirectStandardError (Join-Path $dir 'server.err.log')
for ($i = 0; $i -lt 30 -and -not (Get-Listener); $i++) { Start-Sleep -Milliseconds 250; if ($p.HasExited) { break } }
if (Get-Listener) { Write-Output "$App review running: http://127.0.0.1:$Port/  (PID $($p.Id))" }
else { Write-Output "Failed to start; see $dir\server.err.log"; Get-Content (Join-Path $dir 'server.err.log') -ErrorAction SilentlyContinue | Select-Object -Last 20; exit 1 }
