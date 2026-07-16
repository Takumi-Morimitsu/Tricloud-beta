# release フォルダ内の配布ファイルに対して SHA256SUMS.txt を作成する。
$ErrorActionPreference = "Stop"
$releaseDir = Join-Path (Get-Location) "release"
if (!(Test-Path $releaseDir)) {
  New-Item -ItemType Directory -Path $releaseDir | Out-Null
}
$outFile = Join-Path $releaseDir "SHA256SUMS.txt"
$targets = Get-ChildItem $releaseDir -File -Include *.exe,*.zip,*.msi,*.blockmap -Recurse | Sort-Object FullName
if (!$targets) {
  "No release artifacts found." | Set-Content -Encoding UTF8 $outFile
  exit 0
}
$lines = foreach ($file in $targets) {
  $hash = Get-FileHash -Algorithm SHA256 $file.FullName
  $relative = Resolve-Path -Relative $file.FullName
  "$($hash.Hash.ToLower())  $relative"
}
$lines | Set-Content -Encoding UTF8 $outFile
Write-Host "Created $outFile"
