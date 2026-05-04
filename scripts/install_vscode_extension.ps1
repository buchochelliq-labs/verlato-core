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

function Get-ReleasePilotCli {
  $scriptsDir = if ($IsWindows -or $env:OS -eq "Windows_NT") { "Scripts" } else { "bin" }
  $exeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "releasepilot.exe" } else { "releasepilot" }
  return Join-Path $root ".venv\$scriptsDir\$exeName"
}

function Install-ReleasePilotBackend {
  param(
    [string]$PythonPath
  )

  & $PythonPath -m pip install -e $root
  if ($LASTEXITCODE -eq 0) {
    return
  }

  Write-Warning "Editable backend install failed. Retrying without uninstalling any stale ReleasePilot metadata..."
  & $PythonPath -m pip install -e $root --ignore-installed --no-deps
  if ($LASTEXITCODE -ne 0) {
    throw "ReleasePilot backend install failed. See pip output above."
  }
}

function Assert-ReleasePilotCli {
  param(
    [string]$CliPath
  )

  if (-not (Test-Path -LiteralPath $CliPath)) {
    throw "ReleasePilot CLI was not created at $CliPath. Re-run without -SkipPythonInstall or set releasepilot.cliPath manually."
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
        Write-Warning "Could not update $settingsPath because it is not strict JSON. Add this setting manually: `"releasepilot.cliPath`": `"$CliPath`""
        return
      }
    }
  }

  $settings["releasepilot.cliPath"] = $CliPath
  $settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
  Write-Host "Configured VS Code releasepilot.cliPath: $CliPath"
}

Write-Host "ReleasePilot root: $root"

if (-not $SkipPythonInstall) {
  $venvPython = Get-VenvPython
  if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating ReleasePilot virtual environment..."
    & python -m venv (Join-Path $root ".venv")
  }

  Write-Host "Installing ReleasePilot backend in editable mode into .venv..."
  Install-ReleasePilotBackend -PythonPath $venvPython
  Assert-ReleasePilotCli -CliPath (Get-ReleasePilotCli)
}

Push-Location $extensionRoot
try {
  if (-not $SkipNpmInstall) {
    Write-Host "Installing VS Code extension dependencies..."
    & npm install
  }

  Write-Host "Building VS Code extension..."
  & npm run build

  if (-not $SkipPackage) {
    Write-Host "Packaging VSIX..."
    $packageArgs = @("@vscode/vsce", "package")
    if ($PreRelease) {
      $packageArgs += "--pre-release"
    }
    & npx @packageArgs
  }
}
finally {
  Pop-Location
}

$vsix = Find-Vsix
$code = Find-CodeCommand
$cliPath = Get-ReleasePilotCli

Write-Host "Installing $vsix into VS Code..."
& $code --install-extension $vsix --force

if (-not $SkipConfigureCliPath) {
  if (Test-Path -LiteralPath $cliPath) {
    Set-VsCodeCliPath -CliPath $cliPath
  }
  else {
    Write-Warning "ReleasePilot CLI was not found at $cliPath. Set releasepilot.cliPath manually after installing the backend."
  }
}

Write-Host "ReleasePilot extension installed. Reload VS Code and open the ReleasePilot activity bar view."
