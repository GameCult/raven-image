# Deploy GameCult/raven-image comfy-nodes/ to Raven's ComfyUI. Merging to main deploys.
# Run every 10 minutes by the scheduled task \GameCult\Raven-Image-Deploy, as the logged-in desktop user.
# - Reads main's commit from the forge; does nothing if it is the deployed one.
# - Syncs comfy-nodes/*.py into ComfyUI's custom_nodes, and removes the nodes this script installed earlier and main
#   no longer has (managed.txt). Files it did not install are never touched.
# - If a node file changed: restarts ComfyUI only when its queue is empty; otherwise it leaves state unwritten and
#   tries again next tick. If ComfyUI is not running (Deru closed it), it writes the files and restarts nothing.
$ErrorActionPreference = 'Stop'
$Api = 'https://eureka.gamecult.org/forge/api/v1/repos/GameCult/raven-image'
$Nodes = 'E:\ComfyUI\ComfyUI_windows_portable\ComfyUI\custom_nodes'
$Home_ = 'E:\ComfyUI\raven-image-deploy'
$State = Join-Path $Home_ 'deployed.txt'
$Managed = Join-Path $Home_ 'managed.txt'
$Log = Join-Path $Home_ 'deploy.log'
New-Item -ItemType Directory -Force $Home_ | Out-Null
function Log($m) { Add-Content -Path $Log -Value ("{0:u} {1}" -f (Get-Date), $m) }

try {
  $sha = (Invoke-RestMethod "$Api/branches/main" -TimeoutSec 30).commit.id
  if ($sha -notmatch '^[0-9a-f]{40}$') { Log "no plain sha from the forge: $sha"; exit 1 }
  $old = if (Test-Path $State) { (Get-Content $State -Raw).Trim() } else { '' }
  if ($old -eq $sha) { exit 0 }

  $tmp = Join-Path $env:TEMP ("raven-image-" + $sha.Substring(0, 8))
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  New-Item -ItemType Directory $tmp | Out-Null
  $zip = "$tmp.zip"
  Invoke-WebRequest "$Api/archive/$sha.zip" -OutFile $zip -TimeoutSec 120 -UseBasicParsing
  Expand-Archive $zip -DestinationPath $tmp -Force
  $src = Join-Path $tmp 'raven-image\comfy-nodes'
  $want = @(if (Test-Path $src) { Get-ChildItem $src -Filter '*.py' -File })

  $prev = @(if (Test-Path $Managed) { Get-Content $Managed | Where-Object { $_ } })
  $changed = $false
  foreach ($f in $want) {
    $dest = Join-Path $Nodes $f.Name
    if (-not (Test-Path $dest) -or (Get-FileHash $dest).Hash -ne (Get-FileHash $f.FullName).Hash) { $changed = $true }
  }
  foreach ($n in $prev) { if ($want.Name -notcontains $n -and (Test-Path (Join-Path $Nodes $n))) { $changed = $true } }

  $listening = Get-NetTCPConnection -LocalPort 8190 -LocalAddress 127.0.0.1 -State Listen -ErrorAction SilentlyContinue
  if ($changed -and $listening) {
    $q = Invoke-RestMethod 'http://127.0.0.1:8190/queue' -TimeoutSec 10
    if (@($q.queue_running).Count -gt 0 -or @($q.queue_pending).Count -gt 0) { Log "$($sha.Substring(0,8)): nodes changed, queue busy, retry next tick"; exit 0 }
  }

  foreach ($f in $want) { Copy-Item $f.FullName (Join-Path $Nodes $f.Name) -Force }
  foreach ($n in $prev) { if ($want.Name -notcontains $n) { Remove-Item (Join-Path $Nodes $n) -Force -ErrorAction SilentlyContinue } }
  Set-Content -Path $Managed -Value ($want | ForEach-Object Name)

  if ($changed -and $listening) {
    schtasks /end /tn '\GameCult\Image-Playground' | Out-Null
    Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep 2
    schtasks /run /tn '\GameCult\Image-Playground' | Out-Null
    Log "$($sha.Substring(0,8)): nodes synced, ComfyUI restarted"
  } else {
    Log "$($sha.Substring(0,8)): synced (nodes changed: $changed, ComfyUI running: $([bool]$listening))"
  }
  Set-Content -Path $State -Value $sha
  Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue
} catch {
  Log "failed: $($_.Exception.Message)"
  exit 1
}
