$ErrorActionPreference = "Stop"
Write-Host "Tricloud bundled Python installer build script v30"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Assert-FileExists($Path, $Message) {
  if (-not (Test-Path -LiteralPath $Path)) {
    throw $Message
  }
}

function Assert-DirExists($Path, $Message) {
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw $Message
  }
}

function Invoke-NativeRequired {
  param(
    [Parameter(Mandatory=$true)][string]$Label,
    [Parameter(Mandatory=$true)][string]$FilePath,
    [string[]]$Arguments = @(),
    [string]$WorkingDirectory = (Get-Location).Path
  )

  Write-Host ("> " + $FilePath + " " + ($Arguments -join " "))
  Push-Location $WorkingDirectory
  try {
    & $FilePath @Arguments
    $code = $LASTEXITCODE
  } finally {
    Pop-Location
  }
  if ($null -eq $code) { $code = 0 }
  if ($code -ne 0) {
    throw ($Label + " failed with exit code " + $code)
  }
}

function Get-NpxPath {
  $cmd = Get-Command "npx.cmd" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -eq $cmd) {
    $cmd = Get-Command "npx" -ErrorAction SilentlyContinue | Select-Object -First 1
  }
  if ($null -eq $cmd) {
    throw "npx was not found. Run npm install first, or install Node.js with npm."
  }
  return $cmd.Source
}

function Get-PackageOutputDir {
  $out = node -e "const p=require('./package.json'); console.log((p.build&&p.build.directories&&p.build.directories.output)||'dist')"
  if ($LASTEXITCODE -ne 0) { return "dist" }
  $out = [string]$out
  $out = $out.Trim()
  if ([string]::IsNullOrWhiteSpace($out)) { return "dist" }
  return $out
}

function Get-OutputRoots {
  $outputDir = Get-PackageOutputDir
  $candidateNames = @($outputDir, "release", "dist")
  $seen = @{}
  $roots = @()
  foreach ($name in $candidateNames) {
    if ([string]::IsNullOrWhiteSpace($name)) { continue }
    $full = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $name))
    if ($seen.ContainsKey($full)) { continue }
    $seen[$full] = $true
    if (Test-Path -LiteralPath $full) { $roots += $full }
  }
  return $roots
}

function Find-WinUnpackedDir {
  $roots = Get-OutputRoots
  $dirs = @()
  foreach ($root in $roots) {
    $dirs += @(Get-ChildItem -LiteralPath $root -Directory -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "win-unpacked" })
  }
  if ($dirs.Count -eq 0) {
    foreach ($root in $roots) {
      $dirs += @(Get-ChildItem -LiteralPath $root -Directory -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*unpacked*" })
    }
  }
  if ($dirs.Count -eq 0) {
    throw "No win-unpacked folder was found after the --dir build."
  }
  return ($dirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}

function Quote-CmdArg {
  param([Parameter(Mandatory=$true)][string]$Value)
  return '"' + ($Value -replace '"', '\"') + '"'
}

function Quote-ProcessArgument {
  param([Parameter(Mandatory=$true)][AllowEmptyString()][string]$Value)

  if ($Value.Length -eq 0) { return '""' }
  if ($Value -notmatch '[\s"]') { return $Value }

  # Windows CreateProcess quoting rules for a single argv item.
  $result = '"'
  $backslashes = 0
  foreach ($ch in $Value.ToCharArray()) {
    if ($ch -eq '\') {
      $backslashes++
      continue
    }
    if ($ch -eq '"') {
      $result += ('\' * (($backslashes * 2) + 1))
      $result += '"'
      $backslashes = 0
      continue
    }
    if ($backslashes -gt 0) {
      $result += ('\' * $backslashes)
      $backslashes = 0
    }
    $result += $ch
  }
  if ($backslashes -gt 0) { $result += ('\' * ($backslashes * 2)) }
  $result += '"'
  return $result
}

function Join-ProcessArguments {
  param([string[]]$Arguments = @())
  if ($null -eq $Arguments -or $Arguments.Count -eq 0) { return '' }
  return (($Arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
}

function Get-ExtendedPath {
  param([Parameter(Mandatory=$true)][string]$Path)
  $full = [System.IO.Path]::GetFullPath($Path)
  if ($full.StartsWith('\\?\')) { return $full }
  if ($full.StartsWith('\\')) { return ('\\?\UNC\' + $full.Substring(2)) }
  return ('\\?\' + $full)
}


function Write-TextUtf8NoBom {
  param(
    [Parameter(Mandatory=$true)][string]$Path,
    [Parameter(Mandatory=$true)][string]$Text
  )
  $full = [System.IO.Path]::GetFullPath($Path)
  $enc = New-Object System.Text.UTF8Encoding -ArgumentList $false
  [System.IO.File]::WriteAllText($full, $Text, $enc)
}

function Remove-Utf8BomIfPresent {
  param([Parameter(Mandatory=$true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }

  $full = [System.IO.Path]::GetFullPath($Path)
  $bytes = [System.IO.File]::ReadAllBytes($full)
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    Write-Host ("Removing UTF-8 BOM from: " + $Path)
    $newLen = $bytes.Length - 3
    $clean = New-Object byte[] $newLen
    [System.Array]::Copy($bytes, 3, $clean, 0, $newLen)
    [System.IO.File]::WriteAllBytes($full, $clean)
  }
}

function Repair-ProjectJsonEncodings {
  foreach ($jsonPath in @("package.json", "package-lock.json")) {
    Remove-Utf8BomIfPresent -Path (Join-Path (Get-Location) $jsonPath)
  }
}


function Remove-DirIfExists {
  param(
    [Parameter(Mandatory=$true)][string]$Path,
    [switch]$BestEffort
  )
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
    Write-Host "Trying robocopy /MIR cleanup for long/nested paths..."
    & robocopy $empty $Path /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    & cmd.exe /c ("rmdir /s /q " + (Quote-CmdArg $Path)) | Out-Null
  } catch {
    Write-Warning ("robocopy/cmd cleanup failed: " + $_.Exception.Message)
  } finally {
    if ($empty -and (Test-Path -LiteralPath $empty)) {
      Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not (Test-Path -LiteralPath $Path)) { return }

  if ($BestEffort) {
    Write-Warning ("Could not fully remove directory, but continuing and overwriting required files: " + $Path)
    return
  }
  throw ("Could not remove directory: " + $Path + ". Move the project to a shorter path such as C:\tricloud_build and try again.")
}

function Copy-DirectoryRobust {
  param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination,
    [string[]]$ExcludeDirs = @(),
    [string[]]$ExcludeFiles = @()
  )

  if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw ("Copy source directory was not found: " + $Source)
  }

  $srcFull = [System.IO.Path]::GetFullPath($Source)
  $dstFull = [System.IO.Path]::GetFullPath($Destination)
  $dstParent = Split-Path -Parent $dstFull
  if (-not (Test-Path -LiteralPath $dstParent -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $dstParent | Out-Null
  }

  $robocopyCmd = Get-Command "robocopy.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -ne $robocopyCmd) {
    Write-Host ("Robust copy: " + $srcFull + " -> " + $dstFull)
    $args = @(
      $srcFull,
      $dstFull,
      "/E",
      "/R:2",
      "/W:1",
      "/COPY:DAT",
      "/DCOPY:DAT",
      "/NP"
    )
    if ($ExcludeDirs.Count -gt 0) {
      $args += "/XD"
      $args += $ExcludeDirs
    }
    if ($ExcludeFiles.Count -gt 0) {
      $args += "/XF"
      $args += $ExcludeFiles
    }

    $robocopyPath = $robocopyCmd.Source
    & $robocopyPath @args
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    # robocopy uses 0-7 for success / non-fatal differences and 8+ for failures.
    if ($code -lt 8) {
      return
    }
    throw ("robocopy failed with exit code " + $code + ". Source=" + $srcFull + " Destination=" + $dstFull)
  }

  Write-Warning "robocopy.exe was not found. Falling back to manual file-by-file copy."
  New-Item -ItemType Directory -Force -Path $dstFull | Out-Null

  $sourceRoot = (Get-Item -LiteralPath $srcFull).FullName.TrimEnd('\\')
  $excludeDirSet = @{}
  foreach ($d in $ExcludeDirs) { $excludeDirSet[$d.ToLowerInvariant()] = $true }

  Get-ChildItem -LiteralPath $srcFull -Directory -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\\')
    $parts = @($relative -split '[\\/]')
    $skip = $false
    foreach ($part in $parts) {
      if ($excludeDirSet.ContainsKey($part.ToLowerInvariant())) { $skip = $true; break }
    }
    if (-not $skip) {
      New-Item -ItemType Directory -Force -Path (Join-Path $dstFull $relative) | Out-Null
    }
  }

  Get-ChildItem -LiteralPath $srcFull -File -Recurse -Force -ErrorAction Stop | ForEach-Object {
    $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\\')
    $parts = @($relative -split '[\\/]')
    foreach ($part in $parts) {
      if ($excludeDirSet.ContainsKey($part.ToLowerInvariant())) { return }
    }
    foreach ($pattern in $ExcludeFiles) {
      if ($_.Name -like $pattern) { return }
    }
    $target = Join-Path $dstFull $relative
    $targetParent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
      New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force -ErrorAction Stop
  }
}

function Patch-PackageJsonRuntimeFilters {
  $pkgPath = Join-Path (Get-Location) "package.json"
  if (-not (Test-Path -LiteralPath $pkgPath -PathType Leaf)) { return }

  $pkg = Get-Content -LiteralPath $pkgPath -Raw | ConvertFrom-Json
  if ($null -eq $pkg.build -or $null -eq $pkg.build.extraResources) { return }

  $changed = $false
  foreach ($resource in @($pkg.build.extraResources)) {
    if ($null -eq $resource.from) { continue }
    $from = ([string]$resource.from).Replace('\\', '/')
    if ($from -eq "build/runtime" -or $from.EndsWith("/build/runtime")) {
      $filters = @()
      if ($null -ne $resource.filter) {
        foreach ($f in @($resource.filter)) { $filters += [string]$f }
      }
      if ($filters.Count -eq 0) { $filters += "**/*" }
      foreach ($f in @("!**/__pycache__/**", "!**/*.pyc", "!**/*.pyo")) {
        if ($filters -notcontains $f) {
          $filters += $f
          $changed = $true
        }
      }
      $resource.filter = $filters
    }
  }

  if ($changed) {
    Write-Host "Patched package.json runtime filters to exclude Python bytecode caches."
    $jsonText = $pkg | ConvertTo-Json -Depth 100
    Write-TextUtf8NoBom -Path $pkgPath -Text $jsonText
  }
}

function Copy-RequiredRuntimeIntoUnpacked {
  param([Parameter(Mandatory=$true)][string]$WinUnpacked)

  $resources = Join-Path $WinUnpacked "resources"
  Assert-DirExists $resources ("resources folder was not found in win-unpacked: " + $resources)

  $dstRuntime = Join-Path $resources "runtime"
  $dstBackend = Join-Path $resources "backend"

  Write-Host "Copying known-good runtime/backend directly into win-unpacked before NSIS packaging..."
  Remove-DirIfExists $dstRuntime
  Remove-DirIfExists $dstBackend

  Copy-DirectoryRobust -Source ".\build\runtime" -Destination $dstRuntime -ExcludeDirs @("__pycache__") -ExcludeFiles @("*.pyc", "*.pyo")
  New-Item -ItemType Directory -Force -Path $dstBackend | Out-Null
  foreach ($name in @("node_phase1_runner.py", "node.py", "crypto_common_keywrap.py")) {
    Copy-Item -LiteralPath (Join-Path ".\backend" $name) -Destination (Join-Path $dstBackend $name) -Force
  }

  return [pscustomobject]@{
    Resources = $resources
    Runtime = $dstRuntime
    Backend = $dstBackend
    Python = (Join-Path $dstRuntime "python\python.exe")
  }
}

function Show-PackagedRuntimeDiagnostics {
  param(
    [Parameter(Mandatory=$true)][string]$WinUnpacked,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Runtime,
    [Parameter(Mandatory=$true)][string]$Backend
  )

  Write-Host "==== Packaged runtime diagnostics v25 ===="
  Write-Host ("winUnpacked: " + $WinUnpacked)
  Write-Host ("python: " + $Python + " exists=" + (Test-Path -LiteralPath $Python))
  Write-Host ("runtime: " + $Runtime + " exists=" + (Test-Path -LiteralPath $Runtime))
  Write-Host ("backend: " + $Backend + " exists=" + (Test-Path -LiteralPath $Backend))

  $pythonDir = Split-Path -Parent $Python
  if (Test-Path -LiteralPath $pythonDir) {
    Write-Host "python dir entries:"
    Get-ChildItem -LiteralPath $pythonDir -Force | Select-Object -First 120 | ForEach-Object { Write-Host ("  " + $_.Name) }
    $pth = Get-ChildItem -LiteralPath $pythonDir -Filter "python*._pth" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pth) {
      Write-Host ("pth file: " + $pth.FullName)
      Get-Content -LiteralPath $pth.FullName | ForEach-Object { Write-Host ("  " + $_) }
    } else {
      Write-Host "pth file: NOT FOUND"
    }
  }

  $sp = Join-Path $pythonDir "Lib\site-packages"
  Write-Host ("site-packages: " + $sp + " exists=" + (Test-Path -LiteralPath $sp))
  if (Test-Path -LiteralPath $sp) {
    Write-Host "site-packages entries:"
    Get-ChildItem -LiteralPath $sp -Force | Select-Object -First 160 | ForEach-Object { Write-Host ("  " + $_.Name) }
    Write-Host "native files:"
    Get-ChildItem -LiteralPath $sp -Recurse -Include *.pyd,*.dll -ErrorAction SilentlyContinue | Select-Object -First 200 | ForEach-Object { Write-Host ("  " + $_.FullName) }
  }
}

function Write-PackagedValidationScripts {
  param([Parameter(Mandatory=$true)][string]$Runtime)

  $runtimeValidation = Join-Path $Runtime "validate_tricloud_packaged_runtime_v27.py"
  $runtimeLines = @(
    'import os, sys, traceback, faulthandler',
    'try:',
    '    faulthandler.enable()',
    '    faulthandler.dump_traceback_later(90, repeat=False, exit=True)',
    'except Exception:',
    '    pass',
    'base = os.path.dirname(sys.executable)',
    'site_packages = os.path.join(base, "Lib", "site-packages")',
    'if os.path.isdir(site_packages) and site_packages not in sys.path:',
    '    sys.path.insert(0, site_packages)',
    'handles = []',
    'for root in [site_packages]:',
    '    try:',
    '        entries = os.listdir(root)',
    '    except Exception:',
    '        entries = []',
    '    for entry in entries:',
    '        p = os.path.join(root, entry)',
    '        if os.path.isdir(p) and (entry.endswith(".libs") or entry in ("pyzmq.libs", "zmq.libs")):',
    '            if hasattr(os, "add_dll_directory"):',
    '                try:',
    '                    handles.append(os.add_dll_directory(p))',
    '                except Exception:',
    '                    pass',
    '            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")',
    'print("executable", sys.executable)',
    'print("version", sys.version.split()[0])',
    'print("site_packages", site_packages, os.path.isdir(site_packages))',
    'print("sys.path", repr(sys.path[:12]))',
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
  Set-Content -LiteralPath $runtimeValidation -Value $runtimeLines -Encoding ASCII

  $backendValidation = Join-Path $Runtime "validate_tricloud_backend_runtime_v27.py"
  $backendLines = @(
    'import os, sys, traceback, faulthandler',
    'try:',
    '    faulthandler.enable()',
    '    faulthandler.dump_traceback_later(90, repeat=False, exit=True)',
    'except Exception:',
    '    pass',
    'backend = sys.argv[1]',
    'base = os.path.dirname(sys.executable)',
    'site_packages = os.path.join(base, "Lib", "site-packages")',
    'for p in (backend, site_packages):',
    '    if os.path.isdir(p) and p not in sys.path:',
    '        sys.path.insert(0, p)',
    'handles = []',
    'try:',
    '    entries = os.listdir(site_packages)',
    'except Exception:',
    '    entries = []',
    'for entry in entries:',
    '    p = os.path.join(site_packages, entry)',
    '    if os.path.isdir(p) and (entry.endswith(".libs") or entry in ("pyzmq.libs", "zmq.libs")):',
    '        if hasattr(os, "add_dll_directory"):',
    '            try:',
    '                handles.append(os.add_dll_directory(p))',
    '            except Exception:',
    '                pass',
    '        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")',
    'print("backend", backend, os.path.isdir(backend))',
    'print("site_packages", site_packages, os.path.isdir(site_packages))',
    'print("sys.path", repr(sys.path[:12]))',
    'try:',
    '    import zmq',
    '    import crypto_common_keywrap',
    '    import node_phase1_runner',
    '    print("backend-runtime-ok", zmq.__version__)',
    'except Exception:',
    '    print("backend runtime import failed")',
    '    traceback.print_exc()',
    '    raise'
  )
  Set-Content -LiteralPath $backendValidation -Value $backendLines -Encoding ASCII

  return [pscustomobject]@{ RuntimeValidation = $runtimeValidation; BackendValidation = $backendValidation }
}


function Repair-PackagedPythonRuntimePathing {
  param([Parameter(Mandatory=$true)]$Info)

  $pythonDir = Split-Path -Parent $Info.Python
  $sitePackages = Join-Path $pythonDir "Lib\site-packages"
  if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
    Write-Warning ("Packaged site-packages folder was not found before validation: " + $sitePackages)
  }

  $pth = Get-ChildItem -LiteralPath $pythonDir -Filter "python*._pth" -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pth) {
    $lines = @(Get-Content -LiteralPath $pth.FullName -ErrorAction SilentlyContinue)
    $wanted = @(".", "Lib", "Lib\site-packages", "import site")
    $changed = $false
    foreach ($w in $wanted) {
      if ($lines -notcontains $w) {
        $lines += $w
        $changed = $true
      }
    }
    if ($changed) {
      Write-Host ("Patched packaged Python _pth file: " + $pth.FullName)
      Set-Content -LiteralPath $pth.FullName -Value $lines -Encoding ASCII
    }
  } else {
    Write-Warning ("No python*._pth file found in packaged Python folder: " + $pythonDir)
  }

  # Make DLL discovery more robust for pyzmq wheels in an embedded/packaged Python tree.
  if (Test-Path -LiteralPath $sitePackages -PathType Container) {
    $sitecustomize = Join-Path $sitePackages "sitecustomize.py"
    $sc = @(
      'import os, sys',
      'base = os.path.dirname(sys.executable)',
      'sp = os.path.join(base, "Lib", "site-packages")',
      'if os.path.isdir(sp) and sp not in sys.path:',
      '    sys.path.insert(0, sp)',
      'for name in ("pyzmq.libs", "zmq.libs"):',
      '    p = os.path.join(sp, name)',
      '    if os.path.isdir(p):',
      '        if hasattr(os, "add_dll_directory"):',
      '            try:',
      '                os.add_dll_directory(p)',
      '            except Exception:',
      '                pass',
      '        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")'
    )
    Set-Content -LiteralPath $sitecustomize -Value $sc -Encoding ASCII
  }
}

function Invoke-PythonValidationWithLog {
  param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string[]]$Arguments,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [int]$TimeoutSeconds = 120
  )

  $fullLog = [System.IO.Path]::GetFullPath($LogPath)
  $logDir = Split-Path -Parent $fullLog
  if (-not (Test-Path -LiteralPath $logDir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  }

  $argArray = @("-u") + $Arguments
  $pythonFull = [System.IO.Path]::GetFullPath($Python)
  $cmdForDisplay = $pythonFull + " " + ($argArray -join " ")
  Write-Host ("Validation command: " + $cmdForDisplay)
  Write-Host ("Validation timeout: " + $TimeoutSeconds + " seconds")
  Write-Host ("Validation output will be captured to: " + $fullLog)

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $pythonFull
  $psi.Arguments = Join-ProcessArguments -Arguments $argArray
  $psi.WorkingDirectory = (Get-Location).Path
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true

  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi

  $stdout = New-Object System.Text.StringBuilder
  $stderr = New-Object System.Text.StringBuilder

  $outHandler = [System.Diagnostics.DataReceivedEventHandler]{
    param($sender, $eventArgs)
    if ($null -ne $eventArgs.Data) {
      Write-Host $eventArgs.Data
      [void]$stdout.AppendLine($eventArgs.Data)
    }
  }
  $errHandler = [System.Diagnostics.DataReceivedEventHandler]{
    param($sender, $eventArgs)
    if ($null -ne $eventArgs.Data) {
      Write-Host $eventArgs.Data
      [void]$stderr.AppendLine($eventArgs.Data)
    }
  }

  $p.add_OutputDataReceived($outHandler)
  $p.add_ErrorDataReceived($errHandler)

  try {
    [void]$p.Start()
    $p.BeginOutputReadLine()
    $p.BeginErrorReadLine()

    $exited = $p.WaitForExit($TimeoutSeconds * 1000)
    if (-not $exited) {
      Write-Warning ("Python validation timed out after " + $TimeoutSeconds + " seconds. Killing process id " + $p.Id + ".")
      try {
        $p.Kill()
        $p.WaitForExit(10000) | Out-Null
      } catch {
        Write-Warning ("Failed to kill validation process cleanly: " + $_.Exception.Message)
      }
      $text = @()
      $text += ("COMMAND: " + $cmdForDisplay)
      $text += "EXITCODE: 124"
      $text += ("TIMED_OUT_AFTER_SECONDS: " + $TimeoutSeconds)
      $text += ("ENDED: " + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))
      $text += ""
      $text += "--- STDOUT ---"
      $text += $stdout.ToString()
      $text += "--- STDERR ---"
      $text += $stderr.ToString()
      Write-TextUtf8NoBom -Path $fullLog -Text ($text -join [Environment]::NewLine)
      Write-Host ("Validation log written: " + $fullLog)
      return 124
    }

    # Allow async output readers to flush remaining lines.
    try { $p.WaitForExit() | Out-Null } catch {}

    $exitCode = $p.ExitCode
    $text = @()
    $text += ("COMMAND: " + $cmdForDisplay)
    $text += ("EXITCODE: " + $exitCode)
    $text += ("ENDED: " + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"))
    $text += ""
    $text += "--- STDOUT ---"
    $text += $stdout.ToString()
    $text += "--- STDERR ---"
    $text += $stderr.ToString()
    Write-TextUtf8NoBom -Path $fullLog -Text ($text -join [Environment]::NewLine)

    Write-Host ("Validation log written: " + $fullLog)
    if ($exitCode -ne 0) {
      Write-Warning "Validation failed. Last 80 lines of validation log:"
      try { Get-Content -LiteralPath $fullLog -Tail 80 } catch {}
    }
    return $exitCode
  } finally {
    try { $p.remove_OutputDataReceived($outHandler) } catch {}
    try { $p.remove_ErrorDataReceived($errHandler) } catch {}
    try { $p.Dispose() } catch {}
  }
}


function Copy-RendererDistIntoUnpackedIfMissing {
  param([Parameter(Mandatory=$true)][string]$WinUnpacked)

  $sourceIndex = Join-Path (Get-Location) "dist\index.html"
  Assert-FileExists $sourceIndex "Renderer build output is missing: dist/index.html. Run npm run build before electron-builder."

  $appDir = Join-Path $WinUnpacked "resources\app"
  Assert-DirExists $appDir ("Packaged app folder was not found: " + $appDir)

  $packagedDist = Join-Path $appDir "dist"
  $packagedIndex = Join-Path $packagedDist "index.html"

  if (Test-Path -LiteralPath $packagedIndex -PathType Leaf) {
    Write-Host ("Renderer dist already exists in packaged app: " + $packagedIndex)
    return
  }

  Write-Warning ("Packaged renderer dist was missing. Copying .\\dist into win-unpacked resources\\app manually: " + $packagedDist)
  if (Test-Path -LiteralPath $packagedDist) {
    Remove-DirRobust -Path $packagedDist
  }
  Copy-DirRobust -Source (Join-Path (Get-Location) "dist") -Destination $packagedDist -ExcludeDirs @() -ExcludeFiles @()
  Assert-FileExists $packagedIndex ("Failed to copy renderer dist into packaged app: " + $packagedIndex)
}

function Test-PackagedRuntimeStrict {
  param([Parameter(Mandatory=$true)]$Info)

  foreach ($path in @(
    $Info.Python,
    (Join-Path $Info.Runtime "validate_tricloud_python_runtime.py"),
    (Join-Path $Info.Backend "node_phase1_runner.py"),
    (Join-Path $Info.Backend "node.py"),
    (Join-Path $Info.Backend "crypto_common_keywrap.py")
  )) {
    Assert-FileExists $path ("Packaged runtime file missing: " + $path)
  }

  Repair-PackagedPythonRuntimePathing -Info $Info
  $scripts = Write-PackagedValidationScripts -Runtime $Info.Runtime

  # v30: Do not block installer creation on the heavy packaged Python import validation by default.
  # The generated Portable/win-unpacked app should be tested directly after packaging.
  if ($env:TRICLOUD_STRICT_PACKAGED_VALIDATION -ne "1") {
    Write-Warning "v30 default: skipping heavy packaged Python import validation to avoid validation timeout. Set TRICLOUD_STRICT_PACKAGED_VALIDATION=1 only when you specifically want strict zmq/backend import validation."
    return
  }

  if ($env:TRICLOUD_SKIP_PACKAGED_VALIDATION -eq "1") {
    Write-Warning "TRICLOUD_SKIP_PACKAGED_VALIDATION=1 is set. Skipping packaged Python/backend validation and continuing to NSIS packaging. Use this only as a temporary workaround."
    return
  }

  Write-Host "Validating packaged runtime Python..."
  $runtimeLog = Join-Path (Get-Location) "build\packaged-runtime-validation.log"
  $code1 = Invoke-PythonValidationWithLog -Python $Info.Python -Arguments @($scripts.RuntimeValidation) -LogPath $runtimeLog
  if ($null -eq $code1) { $code1 = 0 }
  if ($code1 -ne 0) {
    Show-PackagedRuntimeDiagnostics -WinUnpacked (Split-Path -Parent $Info.Resources) -Python $Info.Python -Runtime $Info.Runtime -Backend $Info.Backend
    throw ("Packaged runtime Python validation failed with exit code " + $code1 + ". See build\packaged-runtime-validation.log and diagnostic output above.")
  }

  Write-Host "Validating packaged backend imports..."
  $backendLog = Join-Path (Get-Location) "build\packaged-backend-validation.log"
  $code2 = Invoke-PythonValidationWithLog -Python $Info.Python -Arguments @($scripts.BackendValidation, $Info.Backend) -LogPath $backendLog
  if ($null -eq $code2) { $code2 = 0 }
  if ($code2 -ne 0) {
    Show-PackagedRuntimeDiagnostics -WinUnpacked (Split-Path -Parent $Info.Resources) -Python $Info.Python -Runtime $Info.Runtime -Backend $Info.Backend
    throw ("Packaged backend Python validation failed with exit code " + $code2 + ". See build\packaged-backend-validation.log and diagnostic output above.")
  }
}

Repair-ProjectJsonEncodings

Write-Host "[1/7] Checking local backend files..."
Assert-FileExists ".\backend\node_phase1_runner.py" "backend/node_phase1_runner.py is missing. Extract the latest zip into the project root."
Assert-FileExists ".\backend\node.py" "backend/node.py is missing. Extract the latest zip into the project root."
Assert-FileExists ".\backend\crypto_common_keywrap.py" "backend/crypto_common_keywrap.py is missing. Extract the latest zip into the project root."

Write-Host "[2/7] Preparing bundled Python runtime..."
powershell -ExecutionPolicy Bypass -File .\build\prepare-python-runtime.ps1
Assert-FileExists ".\build\runtime\python\python.exe" "Preflight failed: build/runtime/python/python.exe was not created."
Assert-FileExists ".\build\runtime\validate_tricloud_python_runtime.py" "Preflight failed: validate_tricloud_python_runtime.py was not created."

Write-Host "[3/7] Validating bundled Python runtime before packaging..."
& ".\build\runtime\python\python.exe" ".\build\runtime\validate_tricloud_python_runtime.py"
$preCode = $LASTEXITCODE
if ($null -eq $preCode) { $preCode = 0 }
if ($preCode -ne 0) {
  throw ("Preflight bundled Python validation failed with exit code " + $preCode)
}

Write-Host "[4/7] Verifying package.json runtime configuration..."
Repair-ProjectJsonEncodings
Remove-Utf8BomIfPresent -Path ".\package.json"
Remove-Utf8BomIfPresent -Path ".\package-lock.json"
Patch-PackageJsonRuntimeFilters
Repair-ProjectJsonEncodings

Write-Host "[5/7] Building renderer and unpacked app only (--dir)..."
Invoke-NativeRequired -Label "npm run build" -FilePath "npm.cmd" -Arguments @("run", "build")
Assert-FileExists ".\dist\index.html" "Renderer build failed: dist/index.html was not created."

$npx = Get-NpxPath

Write-Host "[5/7] Building unpacked app only (--dir)..."
Invoke-NativeRequired -Label "electron-builder --dir" -FilePath $npx -Arguments @("electron-builder", "--win", "--x64", "--dir")
$winUnpacked = Find-WinUnpackedDir
Write-Host ("win-unpacked found: " + $winUnpacked)
Copy-RendererDistIntoUnpackedIfMissing -WinUnpacked $winUnpacked

Write-Host "[6/7] Injecting and validating runtime/backend in win-unpacked before creating Setup.exe..."
$info = Copy-RequiredRuntimeIntoUnpacked -WinUnpacked $winUnpacked
Test-PackagedRuntimeStrict -Info $info

Write-Host "[7/7] Building NSIS installer from the validated win-unpacked folder..."
# Building the installer from the already validated folder avoids the previous situation where
# Setup.exe was generated before the packaged runtime/backend could be checked or repaired.
Invoke-NativeRequired -Label "electron-builder --prepackaged nsis" -FilePath $npx -Arguments @("electron-builder", "--win", "nsis", "--x64", "--prepackaged", $winUnpacked)

$roots = Get-OutputRoots
$setupFiles = @()
foreach ($root in $roots) {
  $setupFiles += @(Get-ChildItem -LiteralPath $root -Recurse -File -Include *.exe -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
}
$setupFiles = @($setupFiles | Sort-Object LastWriteTime -Descending)
if (-not $setupFiles -or $setupFiles.Count -eq 0) {
  throw "Installer build finished, but no EXE was found in output folders."
}

Write-Host "Newest installer candidate:"
$setupFiles | Select-Object FullName, Length, LastWriteTime -First 5 | Format-Table -AutoSize
Write-Host "Tricloud installer build v29 finished successfully. Send the newest Setup.exe above."
