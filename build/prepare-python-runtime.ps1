param(
  [string]$PythonVersion = "3.11.9",
  [string]$PythonMajorMinor = "3.11",
  [string]$PythonEmbedUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
  [string]$PyzmqVersion = "27.1.0",
  [string]$PyzmqJsonUrl = "https://pypi.org/pypi/pyzmq/27.1.0/json"
)

$ErrorActionPreference = "Stop"

try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
  # Ignore on newer PowerShell versions.
}

$buildDir = $PSScriptRoot
$embedDir = Join-Path $buildDir "python-embed"
$wheelDir = Join-Path $buildDir "python-wheels"
$runtimeRoot = Join-Path $buildDir "runtime"
$runtimeDir = Join-Path $runtimeRoot "python"
$sitePackagesDir = Join-Path $runtimeDir "Lib\site-packages"
$embedZipPath = Join-Path $embedDir ("python-" + $PythonVersion + "-embed-amd64.zip")
$expectedWheelName = "pyzmq-" + $PyzmqVersion + "-cp311-cp311-win_amd64.whl"

function Quote-CmdArg {
  param([Parameter(Mandatory=$true)][string]$Value)
  return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-ExtendedPath {
  param([Parameter(Mandatory=$true)][string]$Path)
  $full = [System.IO.Path]::GetFullPath($Path)
  if ($full.StartsWith('\\?\')) { return $full }
  if ($full.StartsWith('\\')) { return ('\\?\UNC\' + $full.Substring(2)) }
  return ('\\?\' + $full)
}

function Remove-DirIfExists {
  param([Parameter(Mandatory=$true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Write-Host ("Removing directory: " + $Path)

  try {
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
  } catch {
    Write-Warning ("Normal Remove-Item failed: " + $_.Exception.Message)
  }
  if (-not (Test-Path -LiteralPath $Path)) { return }

  try {
    $extended = Get-ExtendedPath -Path $Path
    Remove-Item -LiteralPath $extended -Recurse -Force -ErrorAction Stop
  } catch {
    Write-Warning ("Extended-path Remove-Item failed: " + $_.Exception.Message)
  }
  if (-not (Test-Path -LiteralPath $Path)) { return }

  $empty = $null
  try {
    $empty = Join-Path $env:TEMP ("tricloud_empty_" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    & robocopy $empty $Path /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    & cmd.exe /c ("rmdir /s /q " + (Quote-CmdArg $Path)) | Out-Null
  } catch {
    Write-Warning ("robocopy/cmd cleanup failed: " + $_.Exception.Message)
  } finally {
    if ($empty -and (Test-Path -LiteralPath $empty)) {
      Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  if (Test-Path -LiteralPath $Path) {
    throw ("Could not remove directory: " + $Path + ". Move the project to a shorter path such as C:\\tricloud_build and try again.")
  }
}

function Download-FileWithRetry {
  param(
    [Parameter(Mandatory=$true)][string]$Uri,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [Parameter(Mandatory=$true)][string]$Label,
    [int]$Attempts = 3
  )

  $parent = Split-Path -Parent $OutFile
  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

  $lastErrorMessage = ""
  for ($i = 1; $i -le $Attempts; $i++) {
    try {
      Write-Host ("Downloading " + $Label + " (attempt " + $i + "/" + $Attempts + ")")
      if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force }
      Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
      if (-not (Test-Path -LiteralPath $OutFile)) { throw "Download did not create output file." }
      $length = (Get-Item -LiteralPath $OutFile).Length
      if ($length -le 0) { throw "Downloaded file is empty." }
      return
    } catch {
      $lastErrorMessage = $_.Exception.Message
      Write-Host ("Download failed: " + $lastErrorMessage)
      Start-Sleep -Seconds ([Math]::Min(10, 2 * $i))
    }
  }
  throw ("Failed to download " + $Label + " from " + $Uri + ". Last error: " + $lastErrorMessage)
}

function Extract-ZipFile {
  param(
    [Parameter(Mandatory=$true)][string]$ZipPath,
    [Parameter(Mandatory=$true)][string]$Destination
  )

  New-Item -ItemType Directory -Force -Path $Destination | Out-Null
  try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $Destination)
  } catch {
    throw ("Failed to extract zip file " + $ZipPath + " to " + $Destination + ": " + $_.Exception.Message)
  }
}

function Get-PyZmqWheelFromPyPI {
  param(
    [Parameter(Mandatory=$true)][string]$JsonUrl,
    [Parameter(Mandatory=$true)][string]$OutDir
  )

  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

  if ($env:TRICLOUD_PYZMQ_WHEEL -and (Test-Path -LiteralPath $env:TRICLOUD_PYZMQ_WHEEL)) {
    $dest = Join-Path $OutDir (Split-Path -Leaf $env:TRICLOUD_PYZMQ_WHEEL)
    Copy-Item -LiteralPath $env:TRICLOUD_PYZMQ_WHEEL -Destination $dest -Force
    Write-Host ("Using local pyzmq wheel override: " + $dest)
    return $dest
  }

  Write-Host ("Reading PyPI JSON metadata: " + $JsonUrl)
  $metadata = Invoke-RestMethod -Uri $JsonUrl -UseBasicParsing
  $files = @($metadata.urls)
  if (-not $files -or $files.Count -eq 0) {
    throw "PyPI JSON did not include file URLs."
  }

  $selected = $null
  foreach ($file in $files) {
    if ($null -eq $file) { continue }
    $filename = [string]$file.filename
    if ($filename -eq $expectedWheelName) {
      $selected = $file
      break
    }
  }

  if ($null -eq $selected) {
    $available = ($files | ForEach-Object { [string]$_.filename } | Where-Object { $_ -match "win_amd64\.whl$" }) -join ", "
    throw ("Expected wheel was not found: " + $expectedWheelName + ". Available win_amd64 wheels: " + $available)
  }

  $wheelPath = Join-Path $OutDir ([string]$selected.filename)
  Download-FileWithRetry -Uri ([string]$selected.url) -OutFile $wheelPath -Label ("pyzmq wheel " + $selected.filename)

  $sha256 = [string]$selected.digests.sha256
  if ($sha256) {
    $actual = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $sha256.ToLowerInvariant()) {
      throw ("SHA256 mismatch for " + $selected.filename + ". Expected " + $sha256 + " but got " + $actual)
    }
  }

  Write-Host ("Selected pyzmq wheel: " + $selected.filename)
  return $wheelPath
}

function Write-FileAscii {
  param(
    [Parameter(Mandatory=$true)][string]$Path,
    [Parameter(Mandatory=$true)][string[]]$Lines
  )
  Set-Content -LiteralPath $Path -Value $Lines -Encoding ASCII
}

function Write-SiteCustomize {
  param([Parameter(Mandatory=$true)][string]$SitePackagesDir)

  $siteCustomizePath = Join-Path $SitePackagesDir "sitecustomize.py"
  $content = @'
# Tricloud bundled Python runtime helper.
# This runs automatically because pythonXY._pth contains "import site".
# It keeps native DLL directories from binary wheels visible for imports such as pyzmq.
import os
import sys

_BASE = os.path.dirname(__file__)
_CANDIDATES = []
for name in ("pyzmq.libs", "zmq.libs"):
    _CANDIDATES.append(os.path.join(_BASE, name))

try:
    for entry in os.listdir(_BASE):
        full = os.path.join(_BASE, entry)
        if os.path.isdir(full) and entry.endswith(".libs"):
            _CANDIDATES.append(full)
except Exception:
    pass

_TRICLOUD_DLL_HANDLES = []
for path in _CANDIDATES:
    if not os.path.isdir(path):
        continue
    if hasattr(os, "add_dll_directory"):
        try:
            _TRICLOUD_DLL_HANDLES.append(os.add_dll_directory(path))
        except Exception:
            pass
    old_path = os.environ.get("PATH", "")
    parts = [p for p in old_path.split(os.pathsep) if p]
    if path not in parts:
        os.environ["PATH"] = path + os.pathsep + old_path
'@
  Set-Content -LiteralPath $siteCustomizePath -Value $content -Encoding ASCII
}

function Write-DiagnosticAndThrow {
  param(
    [Parameter(Mandatory=$true)][string]$Message,
    [string]$PythonRuntime,
    [string]$PthPath,
    [string]$SitePackagesDir,
    [string[]]$ValidationOutput = @()
  )

  Write-Host "==== Tricloud bundled Python diagnostic ===="
  Write-Host ("runtimeDir: " + $runtimeDir)
  Write-Host ("python.exe exists: " + (Test-Path -LiteralPath $PythonRuntime))
  if ($PthPath -and (Test-Path -LiteralPath $PthPath)) {
    Write-Host ("pth file: " + $PthPath)
    Get-Content -LiteralPath $PthPath | ForEach-Object { Write-Host ("  " + $_) }
  }
  Write-Host ("site-packages exists: " + (Test-Path -LiteralPath $SitePackagesDir))
  if (Test-Path -LiteralPath $SitePackagesDir) {
    Write-Host "site-packages first entries:"
    Get-ChildItem -LiteralPath $SitePackagesDir -Force | Select-Object -First 80 | ForEach-Object { Write-Host ("  " + $_.Name) }
    Write-Host "native extension files:"
    Get-ChildItem -LiteralPath $SitePackagesDir -Recurse -Include *.pyd,*.dll -ErrorAction SilentlyContinue | Select-Object -First 80 | ForEach-Object { Write-Host ("  " + $_.FullName) }
  }
  if ($ValidationOutput -and $ValidationOutput.Count -gt 0) {
    Write-Host "validation output:"
    $ValidationOutput | ForEach-Object { Write-Host ("  " + $_) }
  }
  if ($PythonRuntime -and (Test-Path -LiteralPath $PythonRuntime)) {
    Write-Host "sys.path and DLL diagnostics:"
    $diagScript = 'import os, sys; print("EXE="+sys.executable); print("PATHS="+repr(sys.path)); print("PATH="+os.environ.get("PATH", ""))'
    & $PythonRuntime -c $diagScript 2>&1 | ForEach-Object { Write-Host ("  " + $_) }
  }
  throw $Message
}

New-Item -ItemType Directory -Force -Path $embedDir | Out-Null
New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

if (-not (Test-Path -LiteralPath $embedZipPath)) {
  Download-FileWithRetry -Uri $PythonEmbedUrl -OutFile $embedZipPath -Label "Python embeddable package"
} else {
  Write-Host ("Python embeddable package already exists: " + $embedZipPath)
}

Write-Host "Removing old bundled runtime..."
Remove-DirIfExists -Path $runtimeDir
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

Write-Host "Extracting Python embeddable package..."
Extract-ZipFile -ZipPath $embedZipPath -Destination $runtimeDir

$pythonRuntime = Join-Path $runtimeDir "python.exe"
if (-not (Test-Path -LiteralPath $pythonRuntime)) {
  throw ("python.exe was not created: " + $pythonRuntime)
}

Write-Host "Configuring Python embeddable path file..."
$pthFiles = Get-ChildItem -LiteralPath $runtimeDir -Filter "python*._pth" -File
if (-not $pthFiles -or $pthFiles.Count -eq 0) {
  throw "python*._pth was not found in the embedded runtime."
}
$pthPath = $pthFiles[0].FullName
$zipLine = "python" + ($PythonMajorMinor.Replace(".", "")) + ".zip"
Write-FileAscii -Path $pthPath -Lines @(
  $zipLine,
  ".",
  "Lib",
  "Lib\site-packages",
  "import site"
)

New-Item -ItemType Directory -Force -Path $sitePackagesDir | Out-Null
Write-SiteCustomize -SitePackagesDir $sitePackagesDir

Write-Host "Downloading pinned pyzmq wheel without using pip..."
Remove-Item -LiteralPath (Join-Path $wheelDir "*.whl") -Force -ErrorAction SilentlyContinue
$wheelPath = Get-PyZmqWheelFromPyPI -JsonUrl $PyzmqJsonUrl -OutDir $wheelDir

Write-Host ("Extracting pyzmq wheel into bundled Python: " + (Split-Path -Leaf $wheelPath))
Extract-ZipFile -ZipPath $wheelPath -Destination $sitePackagesDir

if (-not (Test-Path -LiteralPath (Join-Path $sitePackagesDir "zmq"))) {
  Write-DiagnosticAndThrow -Message "pyzmq wheel was extracted, but site-packages\zmq was not found." -PythonRuntime $pythonRuntime -PthPath $pthPath -SitePackagesDir $sitePackagesDir
}

Write-Host "Validating bundled Python runtime..."
$validationScriptPath = Join-Path $runtimeRoot "validate_tricloud_python_runtime.py"
$validationLines = @(
  'import os',
  'import sys',
  'import traceback',
  'print("executable", sys.executable)',
  'print("version", sys.version.split()[0])',
  'base = os.path.dirname(sys.executable)',
  'site_packages = os.path.join(base, "Lib", "site-packages")',
  'if os.path.isdir(site_packages) and site_packages not in sys.path:',
  '    sys.path.insert(0, site_packages)',
  'dll_handles = []',
  'for name in ("pyzmq.libs", "zmq.libs"):',
  '    p = os.path.join(site_packages, name)',
  '    if os.path.isdir(p):',
  '        if hasattr(os, "add_dll_directory"):',
  '            try:',
  '                dll_handles.append(os.add_dll_directory(p))',
  '            except Exception:',
  '                pass',
  '        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")',
  'try:',
  '    for entry in os.listdir(site_packages):',
  '        p = os.path.join(site_packages, entry)',
  '        if os.path.isdir(p) and entry.endswith(".libs"):',
  '            if hasattr(os, "add_dll_directory"):',
  '                try:',
  '                    dll_handles.append(os.add_dll_directory(p))',
  '                except Exception:',
  '                    pass',
  '            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")',
  'except Exception:',
  '    pass',
  'print("sys.path", repr(sys.path))',
  'print("PATH", os.environ.get("PATH", ""))',
  'try:',
  '    import sitecustomize',
  '    print("sitecustomize ok")',
  'except Exception:',
  '    print("sitecustomize failed")',
  '    traceback.print_exc()',
  'try:',
  '    import zmq',
  '    print("pyzmq", zmq.__version__)',
  '    print("libzmq", zmq.zmq_version())',
  'except Exception:',
  '    print("import zmq failed")',
  '    traceback.print_exc()',
  '    raise'
)
Set-Content -LiteralPath $validationScriptPath -Value $validationLines -Encoding ASCII
$validationOutput = @(& $pythonRuntime $validationScriptPath 2>&1)
$validationExit = $LASTEXITCODE
$validationOutput | ForEach-Object { Write-Host $_ }
if ($validationExit -ne 0) {
  Write-DiagnosticAndThrow -Message "Bundled Python validation failed. import zmq failed." -PythonRuntime $pythonRuntime -PthPath $pthPath -SitePackagesDir $sitePackagesDir -ValidationOutput $validationOutput
}

Write-Host "Prepared embedded Python runtime successfully."
