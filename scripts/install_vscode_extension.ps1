param(
  [switch]$SkipPythonInstall,
  [switch]$SkipNpmInstall,
  [switch]$SkipPackage,
  [switch]$SkipConfigureCliPath,
  [switch]$PreRelease
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$extensionRoot = Join-Path $root "vscode-extension"
$installTemp = Join-Path $root ".tmp\install"
New-Item -ItemType Directory -Force -Path $installTemp | Out-Null
$env:TEMP = $installTemp
$env:TMP = $installTemp
$env:UV_CACHE_DIR = Join-Path $root ".tmp\uv-cache"
$script:PreferredVerlatoCli = $null

function Find-CodeCommand {
  $cmd = Get-Command code.cmd -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }

  $localCode = Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\bin\code.cmd"
  if (Test-Path -LiteralPath $localCode) {
    return $localCode
  }

  $cmd = Get-Command code -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }

  throw "Could not find VS Code's command-line installer. Install the 'code' command or add VS Code to PATH."
}

function Find-ApplicationCommand {
  param(
    [string[]]$Names,
    [string]$DisplayName,
    [string]$InstallHint
  )

  foreach ($name in $Names) {
    $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
    if ($cmd) {
      return $cmd.Source
    }
  }

  throw "Could not find $DisplayName. $InstallHint"
}

function Find-NpmCommand {
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    return Find-ApplicationCommand -Names @("npm.cmd", "npm.exe") -DisplayName "npm" -InstallHint "Install Node.js or add npm.cmd to PATH."
  }
  return Find-ApplicationCommand -Names @("npm") -DisplayName "npm" -InstallHint "Install Node.js or add npm to PATH."
}

function Find-NpxCommand {
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    return Find-ApplicationCommand -Names @("npx.cmd", "npx.exe") -DisplayName "npx" -InstallHint "Install Node.js or add npx.cmd to PATH."
  }
  return Find-ApplicationCommand -Names @("npx") -DisplayName "npx" -InstallHint "Install Node.js or add npx to PATH."
}

function Find-Vsix {
  $latest = Get-ChildItem -LiteralPath $extensionRoot -Filter "*.vsix" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

  if (-not $latest) {
    throw "No VSIX package found in $extensionRoot. Run without -SkipPackage first."
  }

  return $latest.FullName
}

function Get-VenvPython {
  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  $pythonName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "python.exe" } else { "python" }
  return Join-Path $root ".venv\$scriptsDir\$pythonName"
}

function Get-VenvPath {
  return Join-Path $root ".venv"
}

function Get-VerlatoCmdPath {
  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    return Join-Path $root ".venv\$scriptsDir\verlato.cmd"
  }
  return Join-Path $root ".venv\$scriptsDir\verlato"
}

function Get-VerlatoExePath {
  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  return Join-Path $root ".venv\$scriptsDir\verlato.exe"
}

function Get-VerlatoCli {
  if ($script:PreferredVerlatoCli -and (Test-Path -LiteralPath $script:PreferredVerlatoCli)) {
    return $script:PreferredVerlatoCli
  }

  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $exePath = Get-VerlatoExePath
    if (Test-Path -LiteralPath $exePath) {
      return $exePath
    }
  }
  return Get-VerlatoCmdPath
}

function Test-FileLocked {
  param(
    [string]$Path
  )

  if (-not (Test-Path -LiteralPath $Path)) {
    return $false
  }

  $stream = $null
  try {
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    return $false
  }
  catch [System.UnauthorizedAccessException] {
    return $true
  }
  catch [System.IO.IOException] {
    return $true
  }
  finally {
    if ($stream) {
      $stream.Dispose()
    }
  }
}

function Use-VerlatoShimForLockedExe {
  param(
    [string]$PythonPath
  )

  if (-not ($IsWindows -or $env:OS -eq "Windows_NT")) {
    return $false
  }

  $exePath = Get-VerlatoExePath
  if (-not (Test-FileLocked -Path $exePath)) {
    return $false
  }

  Write-Warning "$exePath is locked or not writable, probably because VS Code or a Verlato process is still running. Skipping editable reinstall and using the shim."
  New-VerlatoCliShim -PythonPath $PythonPath
  return $true
}

function New-VerlatoVenv {
  Write-Host "Creating Verlato virtual environment..."
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if ($uv) {
    & $uv.Source venv --seed (Get-VenvPath)
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "uv venv --seed failed. Retrying without seed packages..."
      & $uv.Source venv (Get-VenvPath)
    }
  }
  else {
    & python -m venv (Get-VenvPath)
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create Verlato virtual environment."
  }
}

function Reset-VerlatoVenv {
  $venvPath = Get-VenvPath
  $resolvedRoot = [System.IO.Path]::GetFullPath([string]$root)
  $resolvedVenv = [System.IO.Path]::GetFullPath($venvPath)
  if (-not $resolvedVenv.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove virtual environment outside the repository: $resolvedVenv"
  }

  if (Test-Path -LiteralPath $venvPath) {
    Write-Warning "Recreating generated Verlato virtual environment because pip is not recoverable."
    Remove-Item -LiteralPath $venvPath -Recurse -Force
  }
  New-VerlatoVenv
}

function Install-VerlatoWithUv {
  param(
    [string]$PythonPath
  )

  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if (-not $uv) {
    return $false
  }

  try {
    & $uv.Source pip install --python $PythonPath -e $root
    return $LASTEXITCODE -eq 0
  }
  catch {
    Write-Warning "uv editable install failed: $($_.Exception.Message)"
    return $false
  }
}

function Test-PipAvailable {
  param(
    [string]$PythonPath
  )

  $oldErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & $PythonPath -m pip --version > $null 2>&1
    return $LASTEXITCODE -eq 0
  }
  catch {
    return $false
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
}

function New-VerlatoCliShim {
  param(
    [string]$PythonPath
  )

  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  $shimPath = if ($IsWindows -or $env:OS -eq "Windows_NT") {
    Join-Path $root ".venv\$scriptsDir\verlato.cmd"
  }
  else {
    Join-Path $root ".venv\$scriptsDir\verlato"
  }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $shimPath) | Out-Null

  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $content = @(
      "@echo off",
      "setlocal",
      "set `"PYTHONPATH=$root;%PYTHONPATH%`"",
      "`"$PythonPath`" -m verlato.cli.main %*",
      "exit /b %ERRORLEVEL%"
    )
    $content | Set-Content -LiteralPath $shimPath -Encoding ASCII
  }
  else {
    $content = @(
      "#!/usr/bin/env sh",
      "PYTHONPATH=`"$root`${PYTHONPATH:+:`$PYTHONPATH}`" exec `"$PythonPath`" -m verlato.cli.main `"$@`""
    )
    $content | Set-Content -LiteralPath $shimPath -Encoding UTF8
  }

  $script:PreferredVerlatoCli = $shimPath
  Write-Warning "Created editable Verlato CLI shim at $shimPath because package installation was unavailable."
}

function Install-VerlatoBackend {
  param(
    [string]$PythonPath
  )

  if (Use-VerlatoShimForLockedExe -PythonPath $PythonPath) {
    return
  }

  if (Install-VerlatoWithUv -PythonPath $PythonPath) {
    return
  }

  if (-not (Test-PipAvailable -PythonPath $PythonPath)) {
    New-VerlatoCliShim -PythonPath $PythonPath
    return
  }

  & $PythonPath -m pip install -e $root
  if ($LASTEXITCODE -eq 0) {
    return
  }

  Write-Warning "Editable backend install failed. Repairing pip in the Verlato virtual environment and retrying..."
  & $PythonPath -m ensurepip --upgrade
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "ensurepip could not repair pip. Retrying install anyway..."
  }

  & $PythonPath -m pip install -e $root
  if ($LASTEXITCODE -eq 0) {
    return
  }

  try {
    Reset-VerlatoVenv
    $PythonPath = Get-VenvPython
  }
  catch {
    Write-Warning "Could not recreate the Verlato virtual environment, likely because a running process is holding .venv files: $($_.Exception.Message)"
    New-VerlatoCliShim -PythonPath $PythonPath
    return
  }
  if (Install-VerlatoWithUv -PythonPath $PythonPath) {
    return
  }
  if (-not (Test-PipAvailable -PythonPath $PythonPath)) {
    New-VerlatoCliShim -PythonPath $PythonPath
    return
  }
  & $PythonPath -m pip install -e $root
  if ($LASTEXITCODE -eq 0) {
    return
  }

  Write-Warning "Editable backend install still failed. Retrying without uninstalling any stale Verlato metadata..."
  & $PythonPath -m pip install -e $root --ignore-installed --no-deps
  if ($LASTEXITCODE -ne 0) {
    New-VerlatoCliShim -PythonPath $PythonPath
    return
  }
}

function Assert-VerlatoCli {
  param(
    [string]$CliPath
  )

  if (-not (Test-Path -LiteralPath $CliPath)) {
    throw "Verlato CLI was not created at $CliPath. Re-run without -SkipPythonInstall or set verlato.cliPath manually."
  }
}

function Set-VsCodeCliPath {
  param(
    [string]$CliPath
  )

  $settingsDir = Join-Path $env:APPDATA "Code\User"
  $settingsPath = Join-Path $settingsDir "settings.json"
  New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null

  $settings = [ordered]@{}
  if (Test-Path -LiteralPath $settingsPath) {
    $raw = Get-Content -LiteralPath $settingsPath -Raw
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
      try {
        $parsed = $raw | ConvertFrom-Json
        foreach ($property in $parsed.PSObject.Properties) {
          $settings[$property.Name] = $property.Value
        }
      }
      catch {
        Write-Warning "Could not update $settingsPath because it is not strict JSON. Add this setting manually: `"verlato.cliPath`": `"$CliPath`""
        return
      }
    }
  }

  $settings["verlato.cliPath"] = $CliPath
  try {
    $settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
    Write-Host "Configured VS Code verlato.cliPath: $CliPath"
  }
  catch {
    Write-Warning "Could not write $settingsPath. Add this setting manually: `"verlato.cliPath`": `"$CliPath`""
  }
}

Write-Host "Verlato root: $root"

if (-not $SkipPythonInstall) {
  $venvPython = Get-VenvPython
  if (-not (Test-Path -LiteralPath $venvPython)) {
    New-VerlatoVenv
  }

  Write-Host "Installing Verlato backend in editable mode into .venv..."
  Install-VerlatoBackend -PythonPath $venvPython
  Assert-VerlatoCli -CliPath (Get-VerlatoCli)
}

Push-Location $extensionRoot
try {
  $npm = Find-NpmCommand
  $npx = Find-NpxCommand

  if (-not $SkipNpmInstall) {
    Write-Host "Installing VS Code extension dependencies..."
    & $npm install
    if ($LASTEXITCODE -ne 0) {
      throw "npm install failed. See npm output above."
    }
  }

  Write-Host "Building VS Code extension..."
  & $npm run build
  if ($LASTEXITCODE -ne 0) {
    throw "VS Code extension build failed. See npm output above."
  }

  if (-not $SkipPackage) {
    Write-Host "Packaging VSIX..."
    $packageArgs = @("@vscode/vsce", "package")
    if ($PreRelease) {
      $packageArgs += "--pre-release"
    }
    & $npx @packageArgs
    if ($LASTEXITCODE -ne 0) {
      $existingVsix = Get-ChildItem -LiteralPath $extensionRoot -Filter "*.vsix" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
      if ($existingVsix) {
        Write-Warning "VSIX packaging failed. Reusing existing package: $($existingVsix.FullName)"
      }
      else {
        throw "VSIX packaging failed and no existing VSIX package was found."
      }
    }
  }
}
finally {
  Pop-Location
}

$vsix = Find-Vsix
$code = Find-CodeCommand
$cliPath = Get-VerlatoCli

Write-Host "Installing $vsix into VS Code..."
& $code --install-extension $vsix --force
if ($LASTEXITCODE -ne 0) {
  throw "VS Code extension install failed. See VS Code output above."
}

if (-not $SkipConfigureCliPath) {
  if (Test-Path -LiteralPath $cliPath) {
    Set-VsCodeCliPath -CliPath $cliPath
  }
  else {
    Write-Warning "Verlato CLI was not found at $cliPath. Set verlato.cliPath manually after installing the backend."
  }
}

Write-Host "Verlato extension installed. Reload VS Code and open the Verlato activity bar view."
