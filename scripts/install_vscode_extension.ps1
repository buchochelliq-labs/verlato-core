param(
  [switch]$SkipPythonInstall,
  [switch]$SkipNpmInstall,
  [switch]$SkipPackage,
  [switch]$SkipConfigureCliPath,
  [switch]$PreRelease
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$extensionRoot = Join-Path $root "vscode-extension"
$installTemp = Join-Path $root ".tmp\install"
New-Item -ItemType Directory -Force -Path $installTemp | Out-Null
$env:TEMP = $installTemp
$env:TMP = $installTemp
$env:UV_CACHE_DIR = Join-Path $root ".tmp\uv-cache"

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

function Get-VerlatoCli {
  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $exePath = Join-Path $root ".venv\$scriptsDir\verlato.exe"
    if (Test-Path -LiteralPath $exePath) {
      return $exePath
    }
    return Join-Path $root ".venv\$scriptsDir\verlato.cmd"
  }
  return Join-Path $root ".venv\$scriptsDir\verlato"
}

function New-VerlatoVenv {
  Write-Host "Creating Verlato virtual environment..."
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if ($uv) {
    & $uv.Source venv (Get-VenvPath)
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

  & $uv.Source pip install --python $PythonPath -e $root
  return $LASTEXITCODE -eq 0
}

function Test-PipAvailable {
  param(
    [string]$PythonPath
  )

  & $PythonPath -m pip --version *> $null
  return $LASTEXITCODE -eq 0
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

  Write-Warning "Created editable Verlato CLI shim at $shimPath because package installation was unavailable."
}

function Install-VerlatoBackend {
  param(
    [string]$PythonPath
  )

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

  Reset-VerlatoVenv
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
  if (-not $SkipNpmInstall) {
    Write-Host "Installing VS Code extension dependencies..."
    & npm install
    if ($LASTEXITCODE -ne 0) {
      throw "npm install failed. See npm output above."
    }
  }

  Write-Host "Building VS Code extension..."
  & npm run build
  if ($LASTEXITCODE -ne 0) {
    throw "VS Code extension build failed. See npm output above."
  }

  if (-not $SkipPackage) {
    Write-Host "Packaging VSIX..."
    $packageArgs = @("@vscode/vsce", "package")
    if ($PreRelease) {
      $packageArgs += "--pre-release"
    }
    & npx @packageArgs
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
