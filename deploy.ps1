<#
.SYNOPSIS
    Deploys the working tree (including uncommitted changes) to the Pi.
.DESCRIPTION
    Packs git-tracked and untracked-but-not-ignored files, uploads them to
    <APP_DIR>/releases/<id>, runs tests there, switches the `current` symlink,
    restarts the service and rolls back if /api/status does not answer.
    Settings come from .env (see .env.example).
.EXAMPLE
    ./deploy.ps1
    ./deploy.ps1 -SkipTests
    ./deploy.ps1 -DryRun       # build the archive and list its contents, touch nothing on the Pi
    ./deploy.ps1 -InstallKey   # copy ~/.ssh/id_rsa.pub to the Pi, then password is only needed for sudo
#>
param(
    [switch]$SkipTests,
    [switch]$InstallKey,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# --- config -------------------------------------------------------------
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { throw ".env не найден. Скопируй .env.example в .env и заполни." }
$cfg = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { $cfg[$Matches[1]] = $Matches[2].Trim('"', "'") }
}
function Get-Cfg($name, $default) { if ($cfg[$name]) { $cfg[$name] } else { $default } }
$target   = "$(Get-Cfg JUKEBOX_USER 'jukebox')@$(Get-Cfg JUKEBOX_HOST 'jukebox.local')"
$password = Get-Cfg JUKEBOX_PASSWORD ''
$appDir   = Get-Cfg JUKEBOX_APP_DIR '/home/jukebox/jukebox-manager'
$dataDir  = Get-Cfg JUKEBOX_DATA_DIR '/home/jukebox/jukebox-data'
$port     = Get-Cfg JUKEBOX_PORT '8080'
$keep     = Get-Cfg JUKEBOX_KEEP_RELEASES '5'

# --- ssh password via askpass (Windows OpenSSH has no sshpass) -----------
$work = Join-Path ([IO.Path]::GetTempPath()) ("jukebox-deploy-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $work | Out-Null
$sshOpts = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')
if ($password) {
    $askpass = Join-Path $work 'askpass.cmd'
    Set-Content $askpass '@powershell -NoProfile -NonInteractive -Command "[Console]::Out.Write($env:JUKEBOX_SSH_PASS)"' -Encoding ascii
    $env:JUKEBOX_SSH_PASS = $password
    $env:SSH_ASKPASS = $askpass
    $env:SSH_ASKPASS_REQUIRE = 'force'
    if (-not $env:DISPLAY) { $env:DISPLAY = 'x' }
}

try {
    if ($InstallKey) {
        $pub = Join-Path $HOME '.ssh/id_rsa.pub'
        if (-not (Test-Path $pub)) { throw "Нет $pub" }
        Get-Content $pub | ssh @sshOpts $target 'umask 077; mkdir -p ~/.ssh; tr -d ''\r'' >> ~/.ssh/authorized_keys; sort -u -o ~/.ssh/authorized_keys ~/.ssh/authorized_keys'
        if ($LASTEXITCODE) { throw "Не удалось установить ключ" }
        Write-Host "Ключ установлен в $target"
        return
    }

    # --- release id ------------------------------------------------------
    Push-Location $root
    try {
        $sha = (git rev-parse --short HEAD).Trim()
        $dirty = if (git status --porcelain) { '-dirty' } else { '' }
        $files = git ls-files -co --exclude-standard
    } finally { Pop-Location }
    $release = (Get-Date -Format 'yyyyMMdd-HHmmss') + "-$sha$dirty"

    # --- optional frontend build (frontend/ appears with the React UI) ----
    $frontend = Join-Path $root 'frontend'
    if (Test-Path (Join-Path $frontend 'package.json')) {
        Write-Host "==> сборка фронтенда"
        Push-Location $frontend
        try { npm run build; if ($LASTEXITCODE) { throw "npm run build завершился с ошибкой" } } finally { Pop-Location }
    }

    # --- staging: copy files, force LF in text files -----------------------
    $exclude = '^(\.claude/|\.idea/|\.env|\.git|CLAUDE\.md$|deploy\.ps1$|frontend/)'
    $textExt = '\.(py|sh|service|html|css|js|json|txt|md|cfg|toml)$'
    $stage = Join-Path $work 'stage'
    foreach ($rel in $files) {
        if ($rel -match $exclude) { continue }
        $src = Join-Path $root $rel
        if (-not (Test-Path $src -PathType Leaf)) { continue }  # deleted but still tracked
        $dst = Join-Path $stage $rel
        New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
        if ($rel -match $textExt) {
            $text = [IO.File]::ReadAllText($src) -replace "`r`n", "`n"
            [IO.File]::WriteAllText($dst, $text, [Text.UTF8Encoding]::new($false))
        } else {
            Copy-Item $src $dst
        }
    }
    $dist = Join-Path $frontend 'dist'
    if (Test-Path $dist) { Copy-Item $dist (Join-Path $stage 'static') -Recurse }

    $archive = Join-Path $work 'release.tar.gz'
    tar -czf $archive -C $stage .
    if ($LASTEXITCODE) { throw "tar завершился с ошибкой" }
    Write-Host ("==> релиз {0}, {1:N0} КБ" -f $release, ((Get-Item $archive).Length / 1KB))
    if ($DryRun) {
        tar -tzf $archive
        return
    }

    # --- upload & activate --------------------------------------------------
    $remoteArchive = "/tmp/jukebox-$release.tar.gz"
    scp -q @sshOpts $archive "${target}:$remoteArchive"
    if ($LASTEXITCODE) { throw "scp завершился с ошибкой" }

    $releaseDir = "$appDir/releases/$release"
    $runTests = if ($SkipTests) { '0' } else { '1' }
    $remote = "set -e; mkdir -p '$releaseDir'; tar -xzf '$remoteArchive' -C '$releaseDir'; rm -f '$remoteArchive'; " +
              "APP_DIR='$appDir' DATA_DIR='$dataDir' PORT='$port' KEEP='$keep' RELEASE='$release' RUN_TESTS='$runTests' " +
              "bash '$releaseDir/deploy/remote.sh'"
    # first stdin line = sudo password for remote.sh
    $password | ssh @sshOpts $target $remote
    if ($LASTEXITCODE) { throw "Деплой не удался (код $LASTEXITCODE)" }
    Write-Host "Открыть: http://$(Get-Cfg JUKEBOX_HOST 'jukebox.local'):$port"
}
finally {
    Remove-Item Env:JUKEBOX_SSH_PASS, Env:SSH_ASKPASS, Env:SSH_ASKPASS_REQUIRE -ErrorAction SilentlyContinue
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
